# RAG 效果评估流水线运行与审核说明

本文记录当前版本的迁移顺序、管理员工作流、候选审核 SQL、故障语义和验证结果。
系统按知识库同步运行正式评估，不引入评估运行表、状态提升接口或异步任务。

## 1. 发布前准备

部署必须使用仓库锁定依赖：

```powershell
uv sync --frozen
uv run python -c "import ragas, langchain, langchain_core; print(ragas.__version__, langchain.__version__, langchain_core.__version__)"
```

2026-07-16 实际验证的组合为：

- Python 3.12
- RAGAS 0.4.3
- LangChain 1.3.10
- langchain-core 1.4.8

RAGAS 导入失败时不得启动正式评估。评估复用应用现有回答模型和 Embedding 客户端，
不需要单独的模型、密钥或路由。

已有数据库按以下顺序执行增量 SQL：

1. `app/db/migrations/20260715_extend_eval_dataset.sql`
2. `app/db/migrations/20260715_extend_eval_result.sql`
3. `app/db/migrations/20260715_extend_answer_feedback.sql`
4. `app/db/migrations/20260901_allow_feedback_cancellation.sql`
5. `app/db/migrations/20260902_store_cancelled_feedback_as_zero.sql`
6. `app/db/migrations/20260902_add_chat_intent_metadata.sql`

第一项增加标准问题状态、审核原因和反馈来源；第二项增加可空指标、结果状态和约束；
第三项保存助手回答实际使用的知识库范围并约束反馈值；第四项曾为取消反馈临时放开空值；
第五项将历史空值归一化为 `0`，恢复反馈字段非空，并约束反馈值为 `-1/0/1`。当前取消反馈
会将已有反馈记录的 `feedback` 更新为 `0`，而不是写入 `NULL`。
第六项为 `kb_chat_message` 增加意图路由使用的 `answer_mode` 和 `knowledge_base_searched` 字段；
已有数据库未执行该迁移时，聊天历史查询会因 ORM 模型与实际表结构不一致而失败。
新建环境使用 `app/db/schema.sql`，不要再重复执行增量 SQL。

## 2. 管理员正式评估工作流

所有 `/eval/{kb_id}` 入口都要求系统管理员或该知识库的 `ADMIN` 权限。
调用方可传入安全的 `X-Trace-Id`，也可让服务端生成；响应会回传最终 Trace ID。

### 2.1 查询当前 Chunk

```http
GET /api/v1/eval/{kb_id}/chunks
Authorization: Bearer <token>
```

该接口不分页，只返回当前 `DONE`、未删除文档当前版本的 chunk 摘要和定位信息，
不返回完整正文。管理员从这里选择 `expected_chunk_ids`。

### 2.2 创建标准问题

```http
POST /api/v1/eval/{kb_id}/dataset
Content-Type: application/json

{
  "question": "报销申请应在多久内提交？",
  "expected_answer": "应在三十天内提交。",
  "expected_chunk_ids": [1001, 1002]
}
```

至少填写 `expected_answer` 或 `expected_chunk_ids` 之一。服务会校验每个 chunk 都属于目标
知识库的当前已发布文档版本，人工创建的记录默认进入 `ACTIVE`。

### 2.3 同步运行版本

```http
POST /api/v1/eval/{kb_id}/run?version=release-2026-07-16
```

`eval_version` 在一个知识库内唯一，只标识当前已部署配置，不切换运行参数。每个 `ACTIVE`
问题只执行一次当前 V4 RAG；同一次执行的精排结果、实际上下文和回答共同用于全部指标。
所有问题完成后，逐题结果才在请求事务中一次写入。

### 2.4 查看聚合历史

```http
GET /api/v1/eval/{kb_id}/history
```

历史接口只返回版本级聚合值、各指标样本数、拒答数量和评估时间，不返回 `dataset_id`、
问题正文、逐题结果、实际上下文或内部 RAG 诊断。

## 3. 差评候选的追溯与审核

用户只能反馈自己未删除会话中的助手消息：

```http
POST /api/v1/feedback/{assistant_message_id}
Content-Type: application/json

{
  "feedback": -1,
  "comment": "可选说明"
}
```

再次点击已选反馈时，客户端发送相同路径的 `POST` 请求并传 `{"feedback": null}` 取消反馈；服务端也兼容 `DELETE /api/v1/feedback/{assistant_message_id}`。

反馈表中 `1` 表示有用、`-1` 表示待改进、`0` 表示已取消。单知识库点踩创建或恢复一个
`CANDIDATE`。多知识库、历史消息没有知识库范围，或找不到上一条用户问题时只保存反馈。取消
反馈时，原反馈记录的值改为 `0`、评论清空，尚未审核的候选转为 `ARCHIVED`；记录本身保留，
以保证 `source_feedback_id` 可继续追溯。候选只保存问题和
`source_feedback_id`，回答与引用仍以 `kb_chat_message` 为唯一副本。

管理员可用下列只读 SQL 追溯候选。查询会返回敏感问题、回答、引用和评论，只能在受控
数据库会话中执行，不得复制到日志、监控标签或工单。`:kb_id` 和 `:dataset_id` 是绑定参数。

```sql
SELECT
    dataset.id AS dataset_id,
    dataset.kb_id,
    dataset.status,
    dataset.question,
    feedback.id AS feedback_id,
    feedback.feedback,
    feedback.comment,
    message.id AS assistant_message_id,
    message.content AS assistant_answer,
    message.sources AS assistant_sources
FROM kb_eval_dataset AS dataset
JOIN kb_answer_feedback AS feedback
  ON feedback.id = dataset.source_feedback_id
JOIN kb_chat_message AS message
  ON message.id = feedback.message_id
JOIN kb_chat_session AS session
  ON session.id = message.session_id
WHERE dataset.kb_id = :kb_id
  AND dataset.id = :dataset_id
  AND dataset.status = 'CANDIDATE'
  AND message.role = 'ASSISTANT'
  AND session.is_deleted IS FALSE;
```

## 4. 激活前只读校验

第一版不提供候选或待审核问题转为 `ACTIVE` 的应用接口。直接更新数据库会绕过应用层的
知识库权限、标注校验和不可变性检查，必须先由管理员完成以下步骤：

1. 确认操作者对目标知识库具有 `ADMIN`。
2. 对 `CANDIDATE` 或尚无历史结果的 `NEEDS_REVIEW`，先通过数据集 `PUT` 接口填写问题
   和当前标注，但保持原状态。
3. 执行下列只读 SQL，确认问题非空、至少有一种标注、没有历史评估结果，且每个期望
   chunk 都是目标知识库当前已发布版本。
4. 只有校验全部通过时才执行第 5 节的受约束更新。

```sql
SELECT
    dataset.id,
    dataset.kb_id,
    dataset.status,
    dataset.review_reason,
    btrim(dataset.question) <> '' AS question_valid,
    COALESCE(btrim(dataset.expected_answer), '') <> '' AS has_expected_answer,
    COALESCE(cardinality(dataset.expected_chunk_ids), 0) > 0 AS has_expected_chunks,
    EXISTS (
        SELECT 1
        FROM kb_eval_result AS result
        WHERE result.dataset_id = dataset.id
    ) AS has_history,
    expected.chunk_id,
    CASE
        WHEN expected.chunk_id IS NULL THEN NULL
        ELSE chunk.id IS NOT NULL
          AND chunk.kb_id = dataset.kb_id
          AND document.id IS NOT NULL
          AND document.kb_id = dataset.kb_id
          AND chunk.doc_version = document.version
          AND document.status = 'DONE'
          AND document.is_deleted IS FALSE
    END AS chunk_is_current
FROM kb_eval_dataset AS dataset
LEFT JOIN LATERAL unnest(dataset.expected_chunk_ids) AS expected(chunk_id) ON TRUE
LEFT JOIN kb_doc_chunk AS chunk
  ON chunk.id = expected.chunk_id
LEFT JOIN kb_document AS document
  ON document.id = chunk.doc_id
WHERE dataset.kb_id = :kb_id
  AND dataset.id = :dataset_id
  AND dataset.status IN ('CANDIDATE', 'NEEDS_REVIEW')
ORDER BY expected.chunk_id;
```

满足以下任一情况时禁止激活：

- 查询没有返回行，或问题为空。
- `has_expected_answer` 和 `has_expected_chunks` 都为 `false`。
- `has_history` 为 `true`。
- 任一期望 chunk 的 `chunk_is_current` 不是 `true`。

## 5. 受约束状态更新 SQL

在同一受控变更事务中执行。`SHARE` 表锁会等待已经开始的文档发布完成，并阻止新的文档
或 chunk 写入越过本次校验；随后 SQL 会在新快照中重复第 4 节的关键条件。该锁会短暂阻塞
索引发布，只能在受控运维窗口中持有，并应尽快提交或回滚。

不要把最后的 `COMMIT` 与下列语句一起批量执行。先执行到 `RETURNING`：恰好返回一行时
手工执行 `COMMIT`；返回零行或发生异常时执行 `ROLLBACK`。零行不能改用更宽松的 SQL
重试。

```sql
BEGIN;

LOCK TABLE kb_document, kb_doc_chunk IN SHARE MODE;

UPDATE kb_eval_dataset AS dataset
SET status = 'ACTIVE',
    review_reason = NULL
WHERE dataset.kb_id = :kb_id
  AND dataset.id = :dataset_id
  AND dataset.status IN ('CANDIDATE', 'NEEDS_REVIEW')
  AND btrim(dataset.question) <> ''
  AND (
      COALESCE(btrim(dataset.expected_answer), '') <> ''
      OR COALESCE(cardinality(dataset.expected_chunk_ids), 0) > 0
  )
  AND NOT EXISTS (
      SELECT 1
      FROM kb_eval_result AS result
      WHERE result.dataset_id = dataset.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM unnest(
          COALESCE(dataset.expected_chunk_ids, ARRAY[]::BIGINT[])
      ) AS expected(chunk_id)
      LEFT JOIN kb_doc_chunk AS chunk
        ON chunk.id = expected.chunk_id
       AND chunk.kb_id = dataset.kb_id
      LEFT JOIN kb_document AS document
        ON document.id = chunk.doc_id
       AND document.kb_id = dataset.kb_id
      WHERE chunk.id IS NULL
         OR document.id IS NULL
         OR chunk.doc_version <> document.version
         OR document.status <> 'DONE'
         OR document.is_deleted IS TRUE
  )
RETURNING id, kb_id, status, review_reason;
```

成功且恰好返回一行：

```sql
COMMIT;
```

其他情况：

```sql
ROLLBACK;
```

## 6. 文档重建后的重新标注

成功发布文档新版本时，系统在删除旧 chunk 前，把引用这些 chunk 的 `ACTIVE` 标准问题
置为 `NEEDS_REVIEW`。该状态变化与版本发布在同一事务中；失败或回滚的重建不改变标准
问题。正式评估只读取 `ACTIVE`，因此旧标注不会进入后续指标分母。

若 `NEEDS_REVIEW` 记录已有历史评估结果，不得原地修改或重新激活：

1. 调用 `DELETE /api/v1/eval/{kb_id}/dataset/{dataset_id}` 归档旧记录。
2. 重新调用 chunk 摘要接口选择新版本 chunk。
3. 调用数据集创建接口生成新的 `ACTIVE` 记录。

这样历史结果仍指向不可变的旧问题，新版本评估使用新记录。只有没有历史结果的待审核记录
才可通过 `PUT` 重新标注，并按第 4、5 节审核后激活。

## 7. 降级、拒答与 Trace ID

- Reranker 超时或失败时，V4 使用 RRF 顺序继续裁剪上下文并生成回答。
- 正式评估保留该回答，但 Hit Rate@5、MRR@5 和四项 RAGAS 指标全部为 `NULL`；结果为
  `PARTIAL/reranker_degraded`，且不调用 RAGAS。
- 明确拒答不调用 RAGAS，四项生成指标为 `NULL`，但拒答数量和拒答率仍进入报告。
- 指标不可用时保持 `NULL`，不能转换为零分。

评估、反馈、权限错误、参数错误和未处理异常的 HTTP 响应都带 `X-Trace-Id`。日志格式会
从请求上下文加入同一 Trace ID。日志只记录操作、结果、低基数错误类型、数量和耗时，禁止
记录问题、回答、评论、chunk 正文、Prompt、JWT 或资源 ID。Trace ID 不得作为指标标签。

## 8. 当前同步运行限制

- 一个评估版本在单个 HTTP 请求中串行处理所有 `ACTIVE` 问题。
- 没有持久化运行进度、暂停、取消、恢复或运行级重试。
- 中途进程退出时不会写入部分结果，可使用同一版本重新运行。
- 没有整体运行超时、配置快照、评估成本字段、定时任务或 CI 质量门禁。
- 四项 RAGAS 指标在单题内并发，每项默认超时 60 秒，可通过 `RAGAS_TIMEOUT_SECONDS` 配置，可重试错误最多重试一次。
- 若未来需要异步化，必须新增明确的运行实体并重新设计进度、幂等和历史聚合语义。

## 9. 验证记录

2026-07-16 已执行跨模块回归：

```powershell
uv run pytest -q tests/evaluation tests/repositories/test_evaluations.py tests/repositories/test_feedback_repository.py tests/repositories/test_chat_repository.py tests/services/test_feedback_service.py tests/api/test_feedback_api.py tests/services/test_document_update.py tests/services/test_indexing.py tests/services/test_permissions.py tests/services/test_rag_query_v4.py tests/api/test_evaluation.py tests/api/test_rag.py tests/core/test_trace_id.py
```

结果：`142 passed`，另有一条既有 Starlette `httpx` 弃用警告。

最终验证结果：

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy app
```

- Pytest：`334 passed`，另有一条既有 Starlette `httpx` 弃用警告。
- Ruff：通过。
- Mypy：未通过，报告 13 项既有基线错误；分布在 `app/core/config.py`、
  `app/core/security.py`、`app/repositories/chunks.py`、`app/services/reranker.py`、
  `app/core/exception_handlers.py`、`app/services/document_loader/markdown_parser.py` 和
  `app/core/clients.py`。本次新增集成测试和运行说明没有新增 Mypy 错误。
