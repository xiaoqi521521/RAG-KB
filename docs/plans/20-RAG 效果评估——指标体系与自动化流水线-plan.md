# 20-RAG 效果评估——指标体系与自动化流水线 Plan

本文基于 [20-RAG 效果评估——指标体系与自动化流水线.md](../references/20-RAG%20效果评估——指标体系与自动化流水线.md)、当前 V4 RAG 查询管道以及本次设计访谈形成。本文只描述后续实施方案，不包含代码实现。

本阶段目标是在不复制 RAG 查询链路、不新增数据库表的前提下，补齐标准问题集、同步评估、版本对比、用户反馈候选、文档重建后的标注失效和请求链路追踪。评估结果用于观察不同已部署版本的效果，不改变线上回答，也不作为自动发布门禁。

## 1. 当前状态与缺口

项目已经具备以下基础：

- `RagQueryServiceV4` 已实现查询改写、混合检索、RRF、Reranker 降级、置信度过滤、上下文裁剪、引用和拒答。
- `FaithfulnessEvaluator` 已对线上正常回答做可配置抽样观测，但结果只进入日志和指标，不是可比较的离线评估流水线。
- 数据模型和初始化 SQL 已有 `kb_eval_dataset`、`kb_eval_result`、`kb_answer_feedback` 三张表。
- `kb_chat_message` 已保存用户问题、助手回答、引用、Token、耗时和简单反馈字段。
- `ragas` 已列入项目依赖，当前环境解析为 0.4.3。
- `PermissionService` 已有知识库级读写权限校验，并区分系统管理员和知识库授权。

现有缺口：

- 评估三张表没有 Repository、Service、Schema 或路由实现。
- `kb_eval_dataset` 支持候选、有效、待审核和归档状态；已归档记录允许编辑，
  未归档且已参与评估的记录仍不可原地修改。
- `kb_eval_result` 只能保存 Hit、Rank、Faithfulness 和 Answer Relevancy，不能表达 Context Recall、Context Precision 或单题失败。
- 参考实现为了计算检索指标和生成回答，会对同一问题执行两次查询；忠实性使用的上下文也不一定是生成时的实际参考内容。
- 当前 V4 对外只返回回答和最终引用，评估无法取得置信度过滤前的精排结果以及真正进入模型的参考内容。
- 文档重建会删除旧 chunk，已有 `expected_chunk_ids` 会静默失效。
- 反馈接口尚未实现，助手消息也没有保存本轮请求的知识库范围。
- 当前日志没有请求级 Trace ID。
- 当前 RAGAS 0.4.3 与已安装的 LangChain 组合存在运行时导入故障：RAGAS 导入已从 `langchain-community` 移除的 VertexAI 模块。实施前必须先解决并锁定兼容依赖。

## 2. 已确认的设计决策

| 主题 | 已确认方案 |
| --- | --- |
| 运行方式 | 管理员通过 HTTP 同步触发，问题逐条串行执行 |
| 数据结构 | 不新增数据库表，只扩展现有评估、消息和反馈表字段 |
| 提交方式 | 全部问题执行结束后，在一个事务中批量保存逐题结果 |
| 评估版本 | `eval_version` 在单个知识库内唯一，只对应一次评估运行 |
| 线上一致性 | 评估执行当前已部署的真实 V4 管道，版本名不选择管道或覆盖配置 |
| 共享执行 | 提取共享 RAG 执行模块，一次执行同时产出公开响应与内部评估数据 |
| 检索指标 | Hit Rate@5、MRR@5 |
| 检索取样位置 | Reranker 成功后的 Top 5、置信度过滤和上下文裁剪之前 |
| Reranker 降级 | 继续按 RRF 结果生成回答，但跳过本题全部检索与 RAGAS 指标 |
| 生成指标 | Faithfulness、Answer Relevancy、Context Recall、Context Precision |
| 生成上下文 | 使用真正进入回答模型的参考内容，不使用重新检索或最终引用子集 |
| 指标样本 | 缺少所需标注、指标失败、明确拒答或 Reranker 降级时字段为 `NULL`，不按 0 分处理 |
| 明确拒答 | 不运行四项生成指标；通过固定 `actual_answer` 聚合拒答数，不新增拒答字段 |
| RAGAS 模型 | 复用现有回答模型客户端和 `text-embedding-v3` Embedding 客户端 |
| RAGAS 调用 | 单题内四项指标最多四路并发；每项默认 60 秒超时（`RAGAS_TIMEOUT_SECONDS`），失败后最多重试一次 |
| 单题故障 | 保存 `SUCCESS`、`PARTIAL` 或 `FAILED`，继续后续问题 |
| 全题失败 | 只要结果成功保存，运行接口仍返回 200 和失败摘要 |
| 文档重建 | 在删除旧 chunk 前按 ID 数组重叠查询，把关联问题标为 `NEEDS_REVIEW` |
| 数据集修改 | 未归档且已参与评估的问题仅允许归档，不能修改内容；已归档记录允许编辑 |
| 状态提升 | 首版不提供候选或待审核问题转 `ACTIVE` 的接口，由数据库操作完成 |
| 删除语义 | 只归档，不物理删除标准问题或候选 |
| 反馈幂等 | 同一用户对同一助手消息只保留最新反馈，数据库唯一约束兜底 |
| 候选来源 | 差评候选通过 `source_feedback_id` 追溯原始反馈、回答和引用 |
| 消息配对 | 同一会话内按 `created_at + id` 找到助手回答之前最近的用户问题 |
| 知识库归属 | 助手消息保存本轮 `kb_ids`；单知识库差评自动建候选，多知识库差评只留反馈 |
| 历史查询 | 只提供按版本聚合的历史报告，不提供逐题结果接口 |
| Chunk 标注 | 提供管理员专用查询，首版不分页，只返回摘要和来源元数据 |
| 链路追踪 | 通用 FastAPI 中间件处理 `X-Trace-Id`，写入日志上下文并回写响应头 |

相关领域语言和架构取舍记录在：

- [CONTEXT.md](../../CONTEXT.md)
- [ADR-0004：同步执行评估且不引入评估运行表](../adr/0004-run-evaluations-synchronously-without-run-table.md)
- [ADR-0005：使用 chunk ID 数组保存检索评估真值](../adr/0005-keep-evaluation-ground-truth-as-chunk-id-arrays.md)

## 3. 业务范围

### 3.1 In Scope

- 管理标准问题集及其 `CANDIDATE`、`ACTIVE`、`NEEDS_REVIEW`、`ARCHIVED` 状态。
- 基于当前 V4 管道同步运行单知识库评估。
- 计算 Hit Rate@5、MRR@5 和四项 RAGAS 生成指标。
- 保存逐题结果，并按 `eval_version` 聚合历史报告。
- 文档发布新版本时使引用旧 chunk 的标准问题失效。
- 记录用户点赞、点踩和评论；单知识库差评生成评估候选。
- 保存助手消息本轮 `kb_ids`，支持反馈归属判断。
- 提供不分页的当前 chunk 摘要查询，供管理员标注期望 chunk。
- 增加知识库评估管理权限检查。
- 增加请求 Trace ID 中间件。
- 增加与上述行为匹配的模型、Repository、Service、API 和集成测试。
- 使用 `uv` 解决并锁定可运行的 RAGAS/LangChain 依赖组合。

### 3.2 Out of Scope

- 不新增评估运行表、期望证据关系表或其他数据库表。
- 不实现异步任务、进度查询、暂停、恢复或运行级重试。
- 不实现定时评估、文档更新后自动评估、CI 门禁或指标阈值告警。
- 不允许 `eval_version` 切换管道、模型或临时覆盖检索参数。
- 不保存运行配置快照。
- 不在 `kb_eval_result` 持久化检索耗时、评估耗时或 Token 成本。
- 不设置单次问题数量上限或整个同步运行的总超时。
- 不提供候选或待审核问题转 `ACTIVE` 的公开接口。
- 不提供逐题历史结果查询接口。
- 不给 chunk 标注查询增加分页。
- 不新增前端管理页面。
- 不修改线上固定拒答、引用选择、权限硬过滤或 Reranker 降级行为。
- 不把评估结果用于拦截、重写或重试线上回答。

## 4. 领域状态与生命周期

### 4.1 标准问题状态

`kb_eval_dataset.status` 使用以下状态：

| 状态 | 含义 | 是否参与正式评估 |
| --- | --- | --- |
| `CANDIDATE` | 由差评产生、尚未完成人工标注的候选 | 否 |
| `ACTIVE` | 可用于正式评估的标准问题 | 是 |
| `NEEDS_REVIEW` | 文档重建后期望 chunk 已失效，等待重新标注 | 否 |
| `ARCHIVED` | 已拒绝、删除或被新记录替代的历史问题 | 否 |

状态流转：

```plain
单知识库差评 -> CANDIDATE
人工数据库审核并完成标注 -> ACTIVE
关联文档发布新版本 -> NEEDS_REVIEW
重新标注 -> 归档旧记录 + 创建新的 ACTIVE 记录
删除、拒绝或被替代 -> ARCHIVED
```

应用接口不接受客户端提交 `status`。候选或待审核问题提升为 `ACTIVE` 由管理员直接操作数据库；实施说明必须给出审核 SQL，并要求确认问题非空、至少存在一类标注，所有期望 chunk 均属于目标知识库的当前 `DONE` 文档版本。

### 4.2 可修改性

- 尚未参与评估的 `CANDIDATE`、`NEEDS_REVIEW` 或新建问题可以原地编辑问题、期望答案和期望 chunk。
- 一旦 `kb_eval_result` 已引用某条数据集记录，业务层禁止修改其问题和标注。
- 已参与评估的问题需要修订时，归档旧记录并新建记录，保证历史结果仍指向当时的数据。
- 删除接口始终执行归档；任何状态都不物理删除。

### 4.3 反馈生命周期

```plain
用户提交反馈
  -> 校验助手消息属于当前用户会话
  -> 按 (message_id, user_id) 新增或覆盖反馈
  -> 同步更新助手消息 feedback 字段
  -> feedback = -1 且本轮只有一个 kb_id：创建或恢复 CANDIDATE
  -> feedback = -1 且本轮包含多个或无法确认 kb_id：只保留反馈
  -> feedback 从 -1 改为 1：仍为 CANDIDATE 的关联项转 ARCHIVED
```

候选一旦进入 `ACTIVE` 或 `NEEDS_REVIEW`，后续用户反馈变化不能自动修改它。多知识库反馈若需要纳入标准问题集，由系统管理员人工选择一个目标知识库并通过数据库处理。

## 5. 数据模型变更

本阶段只扩展已有表，不新增表。

### 5.1 `kb_eval_dataset`

保留字段：

```plain
id
kb_id
question
expected_answer
expected_chunk_ids
created_by
created_at
```

新增字段：

```plain
status              VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
review_reason       VARCHAR(50) NULL
source_feedback_id  BIGINT NULL
```

约束与索引：

- `status` 只允许 `CANDIDATE | ACTIVE | NEEDS_REVIEW | ARCHIVED`。
- `source_feedback_id` 唯一且可空，避免同一反馈重复创建候选。
- 按 `(kb_id, status)` 建立查询索引。
- 现有数据迁移为 `ACTIVE`。
- `expected_chunk_ids` 继续存储全局唯一的 `kb_doc_chunk.id`，可以同时包含多个文档的 chunk。

### 5.2 `kb_eval_result`

保留 `dataset_id`、`eval_version`、`actual_answer`、`eval_at`，其中 `eval_version` 使用与 `kb_document.version` 一致的 `INT4` 版本号（例如旧值 `v1_hybrid_reranker` 转为 `1`），并调整为：

```plain
eval_version         INT4 NOT NULL
hit                 BOOLEAN NULL
rank                INT NULL
faithfulness        FLOAT NULL
answer_relevancy    FLOAT NULL
context_recall      FLOAT NULL
context_precision   FLOAT NULL
status              VARCHAR(20) NOT NULL
error_type          VARCHAR(100) NULL
```

约束：

- `status` 只允许 `SUCCESS | PARTIAL | FAILED`。
- 指标分数非空时必须位于 `0.0 ~ 1.0`。
- `rank` 非空时必须大于 0 且不超过 5。
- `(dataset_id, eval_version)` 唯一，作为并发重复运行的数据库兜底。
- `hit=NULL` 表示该题未参与检索指标，不表示未命中。
- Reranker 降级时全部指标必须为空，`status=PARTIAL` 且 `error_type=reranker_degraded`。

`eval_version` 的知识库内唯一性无法仅靠该表的普通唯一约束完整表达，由 Service 在运行前通过数据集关联查询检查；最终批量插入的 `(dataset_id, eval_version)` 唯一约束处理并发竞争。

`error_type` 只保存一个低基数分类：Reranker 降级统一保存 `reranker_degraded`；主流程失败时保存主流程错误类型；部分指标失败时统一保存 `ragas_metric_failed`。具体降级原因和失败指标只进入不含正文的安全日志，避免在单个字符串字段中拼接不受控异常信息。

### 5.3 `kb_chat_message`

新增：

```plain
kb_ids BIGINT[] NULL
```

- 新保存的助手消息写入本轮请求的完整知识库范围。
- 历史消息允许为空；反馈候选无法确认知识库时只记录反馈，不自动建候选。
- 用户问题与助手回答不增加显式父子 ID，继续在同一 `session_id` 内按 `created_at + id` 排序配对。

### 5.4 `kb_answer_feedback`

沿用现有字段，不重复保存问题、模型回答或引用：

```plain
id
message_id
user_id
feedback
comment
created_at
```

数据库和 ORM 模型都必须具备 `(message_id, user_id)` 唯一约束。`feedback` 只允许 `-1`、`0` 或 `1`，其中 `0` 表示用户已取消反馈。

### 5.5 三类回答数据的区分

- 在线用户实际看到的助手回答保存在 `kb_chat_message.content`。
- 反馈通过 `kb_answer_feedback.message_id` 关联该回答。
- 评估运行重新生成的回答保存在 `kb_eval_result.actual_answer`，不得与在线回答混用。

## 6. 共享 RAG 执行模块

### 6.1 设计目标

参考实现先调用 Retriever/Reranker 计算检索指标，再调用完整 RAG Service 生成回答，导致同一问题重复检索且上下文可能不一致。本项目应把当前 V4 编排中的一次完整执行提取为共享模块。

模块提供一个 `execute()` 接口，隐藏检索、降级、裁剪、生成和引用后处理的内部顺序。线上查询模块与评估模块都调用该接口，不允许评估自行拼装第二条查询链路。

概念结果：

```plain
RagExecution
  public_response        # 线上已有 answer/sources/hit_count/latency_ms
  reranked_hits          # Reranker 成功时的排序；降级时保留实际 RRF 结果
  reference_contexts     # 真正进入回答模型的逐条参考内容
  reranker_degraded
  degraded_reason
  explicit_refusal
```

`reranker_degraded` 和 `degraded_reason` 只属于本次执行的内部结果，用于决定是否跳过评估和记录安全日志，不写入 `kb_eval_result`。

`reference_contexts` 必须来自 SourceBuilder 实际注入 Prompt 的内容。如果字符或 Token 预算截断了 chunk，评估只能看到截断后的文本；不能使用原始完整 chunk、最终被模型引用的子集或再次查询得到的内容。

`explicit_refusal` 只是内部执行状态，不写入新数据库字段。保存结果时统一把拒答规范化为项目固定 `RAG_REFUSAL_ANSWER`，历史聚合通过 `actual_answer` 判断拒答。

### 6.2 两个调用方

线上 V4 调用方：

- 调用共享 `execute()`。
- 返回 `public_response`。
- 按现有配置调度线上 20% 忠实性抽样。

评估调用方：

- 调用同一个 `execute()`。
- 使用 `reranked_hits` 计算检索指标。
- 使用 `reference_contexts`、问题、回答和期望答案调用 RAGAS。
- 不调度线上忠实性抽样，避免对同一回答重复评估。
- 内部诊断字段不进入公开 RAG API。

## 7. 指标口径

### 7.1 Hit Rate@5

只对以下题目计算：

- 数据集状态为 `ACTIVE`。
- `expected_chunk_ids` 非空。
- Reranker 没有发生任何降级。

取 Reranker 输出前 5 条的全局 `chunk_id`。任一期望 chunk 出现在 Top 5 即 `hit=true`，否则为 `false`。

```plain
Hit Rate@5 = hit=true 的题数 / hit 非空的题数
```

没有候选但检索链路正常完成时是有效未命中，记 `hit=false`。Reranker 超时、HTTP 错误、空结果、索引映射异常或其他降级时 `hit=NULL`，不进入分母。

### 7.2 MRR@5

在与 Hit Rate@5 相同的题目集合内，取排名最靠前的期望 chunk：

```plain
RR = 1 / rank          # Top 5 内命中
RR = 0                 # Top 5 内未命中
MRR@5 = 所有有效题目的 RR 平均值
```

多个期望 chunk 同时命中时只使用最靠前的排名。Reranker 降级时不计算。

### 7.3 四项生成指标

只有 `expected_answer` 非空、没有明确拒答且 Reranker 未降级时，才尝试生成评估：

| 指标 | 输入 |
| --- | --- |
| Faithfulness | 用户问题、实际回答、实际参考内容 |
| Answer Relevancy | 用户问题、实际回答 |
| Context Recall | 用户问题、期望答案、实际参考内容 |
| Context Precision | 用户问题、期望答案、实际参考内容 |

四项指标均由 RAGAS Adapter 返回 `0.0 ~ 1.0`。单项超时、重试耗尽、异常、`NaN` 或越界值只使该字段为空，不影响其他指标。聚合时每项指标使用自己的非空样本数，不能共用一个总分母。

Reranker 发生任何降级时，本题四项生成指标全部保持 `NULL`，评估模块不得调用 RAGAS。回答仍由线上 V4 按 RRF 降级结果正常生成并保存到 `actual_answer`。

### 7.4 拒答

RAGAS 当前接口对空上下文没有统一的零分语义，Faithfulness、Context Recall 和 Context Precision 可能抛错或产生 `NaN`。因此明确拒答时不调用四项生成指标：

```plain
faithfulness = NULL
answer_relevancy = NULL
context_recall = NULL
context_precision = NULL
actual_answer = 固定拒答文案
```

报告通过固定拒答文案计算 `refusal_count` 和 `refusal_rate`，不新增 `refused` 字段。拒答是一次有效业务结果，不自动记为 `FAILED`。

```plain
refusal_rate = refusal_count / total_questions
```

### 7.5 单题状态

| 状态 | 判定 |
| --- | --- |
| `SUCCESS` | RAG 执行完成，所有适用且应执行的指标均得到有效结果 |
| `PARTIAL` | RAG 执行完成，但 Reranker 降级或至少一个应执行指标失败 |
| `FAILED` | 检索、生成或其他单题主流程异常，无法形成有效业务结果 |

缺少某类标注属于“不适用”，本身不会把结果变成 `PARTIAL`。明确拒答也不是异常；只要其检索流程没有发生导致部分结果的降级，可记为 `SUCCESS`。

## 8. RAGAS Adapter

建议在 `app/evaluation/` 下建立评估模块，RAGAS Adapter 的接口只接收一个标准化样本并返回四个可空分数及稳定错误分类。调用方不直接依赖 RAGAS 的 Dataset、Executor 或供应商对象。

执行规则：

1. 复用现有回答模型客户端和 `text-embedding-v3` Embedding 客户端。
2. 单题四项指标使用 `asyncio.gather(..., return_exceptions=True)` 并发执行。
3. 每项独立设置 60 秒超时（`RAGAS_TIMEOUT_SECONDS`），失败后最多重试一次。
4. 只重试超时、限流和暂时性外部错误；输入错误、解析错误和无效分数不重试。
5. 将异常映射为稳定的 `error_type`，不能保存异常正文、Prompt、问题、回答或参考内容。
6. 对 `NaN`、无穷值和不在 `0.0 ~ 1.0` 的结果按指标失败处理。

依赖前置：

- 实施开始时先增加 RAGAS import smoke test。
- 使用 `uv` 查找并锁定与当前 LangChain/OpenAI 客户端兼容的 RAGAS 版本组合。
- 依赖变更必须同步 `pyproject.toml` 和 `uv.lock`。
- 禁止修改 `.venv/site-packages` 绕过导入问题，也不能在生产代码中伪造兼容模块。

## 9. 同步评估流程

```plain
管理员请求运行评估
  -> 验证 JWT 并实时加载当前用户
  -> require_admin(kb_id)
  -> 校验 eval_version 格式及知识库内唯一性
  -> 读取全部 ACTIVE 标准问题
  -> 无 ACTIVE 问题则拒绝运行
  -> 对每个问题串行执行：
       -> 调用共享 V4 execute()
       -> Reranker 成功且有期望 chunk：计算 Hit@5 / RR@5
       -> Reranker 降级：全部指标留空，不调用 RAGAS
       -> 明确拒答：生成指标留空
       -> Reranker 正常、有期望答案且正常回答：并发执行四项 RAGAS 指标
       -> 形成 SUCCESS / PARTIAL / FAILED 结果
  -> 在一个事务中批量插入所有逐题结果
  -> 聚合本次 eval_version 报告
  -> 返回 200
```

每题在内存中隔离异常并继续下一题。运行期间不保持数据库写事务，不逐题提交；只有全部问题处理结束后才批量写入。进程中断或最终写入失败时不留下半套结果，原 `eval_version` 可以重试。

本阶段不限制问题数量，也不设置运行总超时。四项 RAGAS 的单项超时仍然生效。同步请求可能持续较长时间，且客户端断开或进程重启会丢失本次内存结果，这是不引入评估运行表的已知代价。

## 10. 文档重建后的标注失效

### 10.1 失效时机

只有新文档版本成功构建并准备发布时才使标注失效；任务刚提交或重建失败时不能提前影响当前有效标准问题。

索引发布事务内执行：

```plain
新版本 chunk 已成功写入
  -> 查询该文档当前旧版本的全部 chunk_id
  -> expected_chunk_ids 与旧 ID 数组有重叠的数据集改为 NEEDS_REVIEW
  -> review_reason = document_reindexed
  -> 发布文档新版本
  -> 删除旧版本 chunk
  -> 提交任务完成状态
```

PostgreSQL 使用数组重叠语义：

```sql
expected_chunk_ids && :old_chunk_ids
```

知识库条件和当前状态条件必须同时存在，避免扩大更新范围。只处理 `ACTIVE` 问题；已归档、候选或已待审核记录不重复改写。

### 10.2 重新标注

首版不提供恢复 `ACTIVE` 的接口。管理员应：

1. 查询新版本 chunk 摘要。
2. 创建或编辑新标准问题记录并回填新 `expected_chunk_ids`。
3. 检查所有 chunk 均属于目标知识库的当前 `DONE` 文档版本。
4. 归档旧 `NEEDS_REVIEW` 记录。
5. 通过数据库把新记录设置为 `ACTIVE`。

设计和实施说明必须给出只读校验 SQL 与状态更新 SQL，不能依赖 Prompt 或前端保证标注正确。

## 11. 权限与数据隔离

### 11.1 评估管理权限

`PermissionService` 增加 `require_admin(kb_id, user)`：

- 系统管理员允许。
- 目标知识库上的有效权限必须为 `ADMIN`。
- `READ`、`WRITE` 和公开知识库读权限均不足以管理评估。
- 知识库不存在或已删除返回 404。
- 明确无权返回 403。
- 身份或权限数据源不可用返回 503。

数据集管理、chunk 标注查询、运行和历史报告全部使用该权限。所有按数据集 ID 操作的 Repository 查询还必须包含 `kb_id`，不能先按全局 ID 读取后再在内存判断。

### 11.2 反馈权限

普通认证用户可对自己的助手消息提交反馈：

- 通过 `ChatMessage -> ChatSession` 联查并硬过滤 `ChatSession.user_id` 和未删除状态。
- 目标消息必须为 `ASSISTANT`。
- 不属于当前用户、消息不存在或角色错误统一返回 404，避免泄露消息存在性。
- 反馈提交不能接受客户端提供用户 ID、问题正文、回答正文或知识库 ID。

### 11.3 评估检索范围

评估一次只处理一个 `kb_id`，并继续复用 V4 查询的检索 SQL。向量、全文、RRF、Reranker、上下文裁剪、引用和生成都不得扩大到其他知识库；系统管理员也必须传入明确的单知识库范围。

## 12. API 设计

所有响应沿用项目 `ApiResponse` 信封。以下路径以现有 `/api/v1` 前缀为基础。

### 12.1 评估运行与历史

```plain
POST /api/v1/eval/{kb_id}/run?version={eval_version}
GET  /api/v1/eval/{kb_id}/history
```

运行接口：

- 同步返回本次聚合报告。
- `eval_version` 为正整数版本号，与文档 `version` 字段保持一致；旧的 `vN_*` 标识在迁移时提取为 `N`。
- 同一知识库已有该版本结果时返回 409。
- 没有 `ACTIVE` 问题时返回 409。
- 所有题目结果均为 `FAILED` 时仍返回 200。

历史接口只返回每个版本的聚合报告，按最后评估时间倒序，不提供单题下钻。

### 12.2 数据集管理

```plain
GET    /api/v1/eval/{kb_id}/dataset?status={optional}
POST   /api/v1/eval/{kb_id}/dataset
PUT    /api/v1/eval/{kb_id}/dataset/{dataset_id}
DELETE /api/v1/eval/{kb_id}/dataset/{dataset_id}
```

- GET 可按状态过滤，默认返回全部未物理删除的状态记录。
- POST 用于人工新增标准问题，默认创建 `ACTIVE`；请求体不含 `status`、`created_by` 或 `source_feedback_id`。
- POST 创建 `ACTIVE` 问题时要求问题非空，并至少提供期望答案或期望 chunk；自动候选不走该接口。
- PUT 可编辑尚未参与评估的记录，不接受状态修改。
- 已参与评估的记录 PUT 返回 409，调用方应归档后新建。
- DELETE 只把记录改为 `ARCHIVED`，重复归档保持幂等。
- 新增或更新 `expected_chunk_ids` 时，所有 ID 必须属于该知识库当前版本、`DONE` 且未删除的文档；任一无效则整体返回 422。

### 12.3 Chunk 标注查询

```plain
GET /api/v1/eval/{kb_id}/chunks
```

只返回当前版本、`DONE`、未删除文档的 chunk：

```plain
chunk_id
document_id
document_name
chunk_index
page_number
section_title
token_count
excerpt            # 最多 200 字
```

首版不分页。允许按文档和关键词筛选属于轻量查询能力；如果实现时没有现成安全查询接口，可以先只按文档筛选，不为此引入独立搜索抽象。完整 chunk 正文不返回。

### 12.4 用户反馈

```plain
POST   /api/v1/feedback/{message_id}
DELETE /api/v1/feedback/{message_id}
```

`POST` 请求包含 `feedback=1|-1` 和可选评论；客户端取消时传 `feedback=null`，`DELETE` 也可取消当前用户已有反馈。Service 在同一事务中：

1. 校验消息所有权和助手角色。
2. 提交时新增或更新 `kb_answer_feedback`；取消时将其反馈值改为 `0` 并清空评论，以保留候选来源追溯。
3. 同步更新助手消息的 `feedback` 字段，取消时仍写入 `NULL` 供前端判断未选中状态。
4. 按本轮 `kb_ids` 和反馈变化创建、恢复或归档候选。

## 13. 聚合报告

每个 `eval_version` 返回：

```plain
kb_id
eval_version
total_questions
success_count
partial_count
failed_count
retrieval_sample_count
hit_count
hit_rate_at_5
mrr_at_5
faithfulness_sample_count
avg_faithfulness
answer_relevancy_sample_count
avg_answer_relevancy
context_recall_sample_count
avg_context_recall
context_precision_sample_count
avg_context_precision
refusal_count
refusal_rate
eval_at
```

聚合规则：

- Hit Rate 和 MRR 只使用 `hit IS NOT NULL` 的结果。
- 未命中题的 MRR 项为 0；Reranker 降级题不进入分母。
- 四项生成指标分别使用非空字段聚合并分别返回样本数。
- 拒答数通过 `actual_answer` 等于固定拒答文案计算。
- 拒答率以本版本 `total_questions` 为分母；没有题目时不会产生评估版本。
- 报告不把空指标转换为 0；没有样本时平均值返回 `NULL`。
- 不持久化报告，由 Repository 根据逐题结果实时聚合。
- 不同版本的数据集可能发生变化，调用方必须结合各项样本数解释结果；首版不提供交集问题集对比。

## 14. Trace ID 与可观测性

### 14.1 请求中间件

新增通用 FastAPI 中间件：

1. 读取 `X-Trace-Id`。
2. 客户端值符合长度和安全字符约束时沿用，否则生成新的 UUID Trace ID。
3. 写入请求级上下文，供标准 logging 使用。
4. 在响应头回写 `X-Trace-Id`。
5. `finally` 清理上下文，避免异步请求串号。

Trace ID 可进入日志，但不能作为 Prometheus/OpenTelemetry 指标标签。

### 14.2 日志

允许记录：

- 操作类型、题目数量、成功/部分/失败数量。
- 指标是否成功、错误类型、Reranker 是否降级及原因分类。
- 运行和单项指标耗时。
- 反馈结果和候选状态变化类型。

禁止记录：

- JWT、用户 ID、知识库 ID、文档 ID、消息 ID、对象路径等敏感高基数标识。
- 完整问题、期望答案、实际回答、反馈评论、参考内容或 chunk 正文。
- RAGAS Prompt、API Key、连接地址和异常响应正文。

### 14.3 指标

可记录低基数运行指标：

- 评估运行次数和运行状态。
- 逐题 `SUCCESS/PARTIAL/FAILED` 数量。
- RAGAS 单项成功、超时、重试耗尽和解析失败数量。
- Reranker 降级数量。
- 用户反馈正负数量和候选创建数量。

不以 `eval_version`、Trace ID、用户、知识库、文档或消息标识作为指标标签。Token 继续由现有 `TokenMetrics` 观测，但不写入 `kb_eval_result`，因此历史报告不支持按版本比较成本。

## 15. 失败语义

| 场景 | 行为 |
| --- | --- |
| 知识库不存在 | 404，不执行评估 |
| 无知识库 ADMIN 权限 | 403，不执行评估 |
| 身份或权限数据源不可用 | 503，不执行评估 |
| `eval_version` 已存在 | 409，不执行评估 |
| 没有 ACTIVE 问题 | 409，不执行评估 |
| 单题检索或生成异常 | 该题 `FAILED`，继续下一题 |
| Reranker 任意降级 | 使用 RRF 生成；全部指标为空；不调用 RAGAS；题目为 `PARTIAL` |
| 单项 RAGAS 失败 | 该指标为空；其他指标继续；题目至少 `PARTIAL` |
| 明确拒答 | 生成指标为空，记录固定回答，不自动失败 |
| 所有题目失败 | 批量保存并返回 200 失败报告 |
| 最终数据库写入失败 | 整批回滚并返回 500 |
| 进程中断或客户端断开 | 不保证保存内存结果；允许原版本重试 |
| 反馈目标不属于当前用户 | 404 |
| 期望 chunk 跨知识库或已过期 | 422，整次数据集写入失败 |

## 16. 推荐代码组织

遵循现有分层，不把全部逻辑写入 Controller：

```plain
app/
  api/
    middleware/
      trace_id.py
    routes/
      evaluations.py
      feedback.py
  evaluation/
    metrics.py              # Hit@5、MRR@5 纯计算
    ragas_evaluator.py      # RAGAS Adapter
    service.py              # 同步评估编排与报告生成
  repositories/
    evaluations.py
    feedback.py
  schemas/
    evaluation.py
    feedback.py
  services/
    rag_execution.py        # 线上与评估共享的 V4 执行模块
    feedback.py
```

模块原则：

- `RagExecution` 是线上问答和评估的共享内部结果，不是公开响应 Schema。
- 纯指标计算模块只接收 chunk ID 排名，不访问数据库或模型。
- RAGAS Adapter 隐藏具体库调用、超时、重试和分数校验。
- Evaluation Repository 负责数组重叠、版本检查、批量保存和聚合 SQL。
- Feedback Repository 负责消息所有权联查、幂等反馈和候选关联。
- 权限、反馈候选、文档失效和报告聚合不得塞进 Prompt。

## 17. 测试计划

### 17.1 纯指标

- 期望 chunk 在第 1、5 位时命中。
- 期望 chunk 在第 6 位或不存在时未命中。
- 多个期望 chunk 取最靠前排名。
- 未命中 RR 为 0。
- 空候选正常得到未命中。
- Reranker 降级由编排层跳过指标，不调用计算器伪造 0 分。

### 17.2 RAGAS Adapter

- 四项指标使用正确输入字段。
- 复用现有模型与 Embedding 客户端。
- 四项指标在单题内并发且相互隔离。
- 单项默认超时 60 秒（`RAGAS_TIMEOUT_SECONDS`）并最多重试一次。
- 非暂时性错误不重试。
- `NaN`、无穷值和越界值按失败处理。
- 拒答和无期望答案时不调用 RAGAS。
- Reranker 降级时不调用 RAGAS。
- 增加真实 import smoke test，确保锁定依赖能够加载目标 RAGAS 类型。

### 17.3 共享 RAG 执行

- 线上和评估只执行一次检索、精排和生成。
- `reranked_hits` 是过滤前的精排 Top 5。
- 降级时内部保存实际 RRF 结果和降级原因。
- `reference_contexts` 与实际 Prompt 内容一致，遵守截断和 Token 预算。
- 评估调用不触发线上忠实性抽样。
- 公开 RAG 响应不暴露内部评估字段。

### 17.4 评估编排

- 只加载 `ACTIVE` 问题。
- 缺少期望 chunk 时检索字段为空且不算失败。
- 缺少期望答案时生成字段为空且不算失败。
- Reranker 降级时生成继续、全部指标为空、不调用 RAGAS、状态为 `PARTIAL`。
- 单项 RAGAS 失败只影响对应字段。
- 单题主流程失败后继续下一题。
- 所有题失败仍形成可保存报告。
- 全部题完成后只执行一次批量保存。
- 保存失败时整批回滚。
- 同一知识库重复 `eval_version` 返回冲突。

### 17.5 数据集与重建

- `expected_chunk_ids` 可包含不同文档的全局 chunk ID。
- 跨知识库、旧版本、非 `DONE` 或已删除文档 chunk 被拒绝。
- 已参与评估的问题不能原地修改。
- 删除只归档。
- 新版本发布时，只把与旧 chunk ID 数组重叠的 `ACTIVE` 问题改为 `NEEDS_REVIEW`。
- 重建失败时标准问题状态不变。
- 标注失效、文档版本发布和旧 chunk 删除在同一数据库事务内生效。

### 17.6 反馈

- 只能反馈当前用户会话中的助手消息。
- 他人消息、用户消息和不存在消息统一返回 404。
- 重复反馈覆盖而不新增第二行。
- 单知识库点踩创建一个候选，并通过 `source_feedback_id` 去重。
- 多知识库或历史无 `kb_ids` 消息只记录反馈。
- 点踩改点赞时仅归档仍为 `CANDIDATE` 的记录。
- 已进入正式生命周期的记录不随反馈变化。
- 同一会话按 `created_at + id` 取得最近用户问题。

### 17.7 权限、API 与 Trace

- 系统管理员和知识库 `ADMIN` 可管理评估。
- `READ`、`WRITE` 和公开读权限不能管理评估。
- 数据集 ID 不能跨知识库读取或修改。
- 历史接口只返回聚合结果，不返回逐题内容。
- chunk 查询只返回当前有效 chunk 摘要，不返回完整正文。
- 客户端合法 Trace ID 被沿用并回写。
- 缺失或非法 Trace ID 被替换。
- 并发请求日志上下文不串号，响应后上下文被清理。
- Trace ID 不进入指标标签。

## 18. 推荐实施顺序

后续实现建议采用 TDD，并按以下顺序推进：

1. 先解决 RAGAS 依赖兼容并加入 import smoke test。
2. 增加数据模型迁移、约束和 Repository 数组查询测试。
3. 实现 Hit@5/MRR@5 纯指标模块。
4. 提取共享 `RagExecution`，保持现有 V4/API 回归测试通过。
5. 实现 RAGAS Adapter 的并发、超时、重试和结果校验。
6. 实现同步评估编排、批量保存和历史聚合。
7. 接入文档发布时的标注失效。
8. 实现消息 `kb_ids`、反馈幂等和候选联动。
9. 增加 `require_admin` 与评估、反馈路由。
10. 增加 Trace ID 中间件和日志上下文。
11. 运行相关测试、全量测试、Ruff 和 Mypy，并补充实际执行过程文档。

## 19. 验收标准

- 管理员可以维护标准问题、查询当前 chunk、同步运行评估并比较版本报告。
- 每个问题只执行一次真实 V4 管道，检索排名和生成上下文来自同一次执行。
- Hit Rate@5 和 MRR@5 只计算 Reranker 成功且已标注期望 chunk 的题目。
- Reranker 任意降级时回答仍按 RRF 生成，全部指标为空且不会调用 RAGAS。
- 四项 RAGAS 指标只使用实际回答、期望答案和真正进入模型的参考内容。
- 单项指标失败不影响其他指标，单题失败不影响其他题目。
- 明确拒答不产生误导性的 RAGAS 分数，报告能展示拒答数量和比例。
- 全部结果在一次事务中写入，不留下正常中断产生的半套版本数据。
- 文档新版本发布后，引用旧 chunk 的问题不再参与正式评估。
- 差评可以追溯到用户实际看到的回答，且不会重复保存回答正文。
- 多知识库反馈不会被自动错误归属到某一个标准问题集。
- 所有评估管理操作执行知识库级 ADMIN 硬校验，反馈执行会话所有者硬过滤。
- 日志和指标不记录问题、回答、正文、评论或敏感高基数业务标识。
- 每个 HTTP 响应带可追踪的 `X-Trace-Id`，并且请求间上下文隔离。
- RAGAS 在锁定依赖环境中可以真实导入和执行，未通过 smoke test 不得宣称流水线可用。

## 20. 验证命令

实施完成后至少执行：

```powershell
uv run pytest tests/evaluation -v
uv run pytest tests/services/test_rag_query_v4.py tests/api/test_rag.py -v
uv run pytest tests/services/test_indexing.py tests/services/test_feedback.py -v
uv run pytest tests/api/test_evaluations.py tests/api/test_feedback.py -v
uv run pytest tests/core/test_trace_id.py -v
uv run pytest -v
uv run ruff check .
uv run mypy app
```

如果实施阶段尚未创建对应测试文件，应先按上述测试计划补齐。Ruff、Mypy、真实模型或数据库集成测试未运行时必须如实说明，不能把依赖解析成功等同于功能验证通过。
