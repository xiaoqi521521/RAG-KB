# 12-基础 RAG 查询管道——检索与生成 Plan

本文基于 [12-基础 RAG 查询管道——检索与生成.md](/D:/Code/python/Practical_Project/rag-kb/docs/references/12-基础%20RAG%20查询管道——检索与生成.md) 和 [三阶段生成.md](/D:/Code/python/Practical_Project/rag-kb/docs/prompt/三阶段生成.md) 的 Plan 阶段规则，梳理 LangChain / FastAPI 版本中的基础在线查询管道计划。

本阶段目标是先跑通最小但可上线演进的 RAG 查询闭环：用户提问 -> 权限校验 -> 问题向量化 -> PGVector 向量召回 -> 组装受约束 Prompt -> LLM 生成回答 -> 返回答案和基础引用。它不是混合检索、Reranker、上下文裁剪、SSE 流式输出或多轮对话的完整方案；这些能力按参考资料后续章节分阶段补齐。

## 1.1 任务背景

离线索引链路已经具备文档上传、解析、分块、Embedding、chunk 入库和重建索引等基础能力。当前项目中可复用的关键基础包括：

- `EmbeddingService.embed_query(...)`：可以将单条用户问题向量化，并复用 Redis embedding 缓存、维度校验和 provider 重试能力。
- `DocChunk` 模型：已包含 `kb_id`、`doc_id`、`chunk_index`、`content`、`embedding`、`page_num`、`section_title`、`token_count` 和 `doc_version`。
- `KbDocument.version`：已支持文档重建后的当前版本号，查询侧必须只召回当前版本 chunk。
- `PermissionService.require_read(...)`：已具备知识库读权限校验能力，查询入口必须在检索前完成硬校验。
- `ChatOpenAI` 客户端初始化：`app/core/clients.py` 已通过 OpenAI-compatible API 初始化聊天模型。
- `Settings` 中已有 `rag_vector_top_k`、`rag_return_top_n`、`rag_min_score`、`rag_context_max_tokens`、`chat_model`、`chat_temperature` 和 `chat_max_tokens` 等配置；当前实现保留 `rag_min_score` 配置但暂不用于过滤候选。

基础查询管道要解决的核心问题是：在不引入后续增强复杂度的前提下，让前端或 cURL 能通过 HTTP 接口向一个或多个有权限知识库提问，并得到基于检索上下文的中文回答。回答必须具备两个企业级底线：

- 权限隔离发生在检索层，不能把“只能看某知识库”写成 Prompt 规则。
- 知识库没有足够上下文时明确拒答，不能让模型用通用知识补全公司政策或内部制度。

参考资料第 12 章给出的 Spring AI 流程是“向量检索 -> 组装 Prompt -> 模型生成”。迁移到当前 Python 项目时，需要把 Spring `ChatClient` 替换为已有 LangChain `ChatOpenAI` 客户端，把 JPA Repository 替换为 SQLAlchemy async Repository，并补齐当前项目 AGENTS.md 要求的权限过滤、引用元数据和可观测日志。

## 1.2 业务流程

### 1.2.1 总体执行链路

基础 RAG 查询流程：

```plain
1. 接收查询请求：`question`、`kb_ids`、可选 `session_id`
2. 校验输入和读权限：问题不能为空；每个 `kb_id` 都必须通过 `PermissionService.require_read`
3. 向量化问题：调用 `EmbeddingService.embed_query(question)` 得到查询向量
4. 执行向量检索：在 `kb_doc_chunk` 中按允许的 `kb_id`、当前 `doc_version` 和向量距离召回 TopK
5. 无召回处理：没有检索结果时直接返回“未找到相关内容”；低分候选当前不按阈值过滤
6. 组装上下文和引用：按召回顺序拼接 chunk 内容，并保留文档、页码、章节和 chunk 标识
7. 调用聊天模型：使用只允许基于参考内容回答的 System Prompt 生成答案
8. 返回查询结果：返回 answer、sources、命中数量、耗时等基础字段；必要时写入会话消息记录
```

### 1.2.2 完整业务流程

#### 查询入口

本阶段建议新增独立 RAG 查询入口，不塞进知识库管理路由：

```plain
POST /api/v1/rag/query
```

请求体至少包含：

```json
{
  "question": "代码提交需要遵守什么规范？",
  "kb_ids": [1, 2],
  "session_id": null
}
```

入口层职责保持轻量：

- 使用现有认证依赖获取 `CurrentUser`。
- 校验 `question` 去空白后非空。
- 校验 `kb_ids` 非空、去重后数量在合理范围内。
- 对每个 `kb_id` 调用 `PermissionService.require_read(...)`。
- 调用服务层完成检索与生成。

权限校验必须发生在向量检索之前。如果用户传入多个知识库，只要其中一个无权限，建议直接返回 403 或 404，而不是静默跳过；这样可以避免调用方误以为查询覆盖了全部知识库。后续 Spec 可以结合产品体验最终确认错误码策略，但不得允许无权限知识库参与检索。

#### 问题向量化

查询服务调用 `EmbeddingService.embed_query(question)` 生成查询向量。该方法内部已经复用批量向量化、缓存、维度校验和 provider 重试能力，因此基础查询管道不再新增单独的 query embedding 客户端。

需要保留两个边界：

- 建库和查询必须使用同一 embedding 模型与维度，当前模型维度由 `EmbeddingConfig.dimension` 和 `Settings.embedding_dimension` 控制。
- 如果问题为空、provider 失败或向量维度不匹配，应返回明确错误，不进入检索和生成阶段。

#### 向量检索

本阶段只做 PGVector 向量检索，不做全文检索、RRF 和 Reranker。检索 SQL 必须具备硬过滤：

```plain
kb_doc_chunk.kb_id IN allowed_kb_ids
kb_doc_chunk.doc_version = kb_document.version
kb_document.is_deleted = false
kb_document.status = DONE
```

排序使用 PGVector 距离：

```plain
ORDER BY kb_doc_chunk.embedding <=> :query_vector
LIMIT :top_k
```

Repository 返回结果时应携带距离转换后的 `score`，用于排序展示、日志和后续阈值方案评估。当前实现不再用 `rag_min_score` 过滤候选，低分 chunk 仍可进入 `SourceBuilder`，最终进入 Prompt 的数量由 `rag_return_top_n` 和上下文预算控制。

跨多个知识库查询时，不应像参考 Java 基础版那样先每个知识库各取 TopK 再简单截断；Python 版本建议一次 SQL 在允许 `kb_ids` 范围内全局排序，避免某个知识库结果过多挤掉更相关结果。后续混合检索阶段再引入向量 TopK、全文 TopK 和 RRF 融合。

#### 无召回和拒答

基础阶段在生成前只做无召回拒答：

```plain
检索结果为空
  -> 不调用聊天模型
  -> 返回“在知识库中未找到相关内容”
```

`rag_min_score` 暂时只作为后续“相似度阈值过滤”方案的保留配置，不参与当前基础查询管道的候选过滤。后续重新启用阈值时，需要单独明确分数公式、过滤位置、日志字段和用户可见行为。

生成阶段还需要第二层拒答：Prompt 明确要求模型只根据参考内容回答；参考内容不足时必须说明“未找到相关内容”。这层不能替代检索前的权限和低召回判断，只作为防幻觉兜底。

#### 上下文与引用组装

服务层将召回 chunk 转换为两类数据：

```plain
上下文文本：
[参考1] 文档：员工手册.md；页码：3；章节：请假流程
chunk 内容...

[参考2] 文档：研发规范.pdf；页码：8；章节：代码提交
chunk 内容...
```

```plain
引用 sources：
document_id
document_name
kb_id
chunk_id
chunk_index
page_number 或 section_title
score
```

当前 `DocChunk` 只有 `doc_id` 和 `kb_id`，文档名需要 join `kb_document.file_name`。即使本阶段的前端暂不展示引用，也应在 API 响应中返回基础 sources，避免后续引用溯源阶段返工。

基础阶段暂不做复杂 token 裁剪，但必须设置保守上限：最多拼接 `rag_return_top_n` 个 chunk，且上下文总长度不能无限增长。精确 token 预算和更细致裁剪放到第 16 章对应阶段。

#### Prompt 生成

System Prompt 应沿用参考资料的核心规则，并贴合企业知识库场景：

```plain
你是企业内部知识库助手。你只能根据【参考内容】回答用户问题。
如果参考内容不足以回答，必须明确说明“在知识库中未找到相关内容”，不要使用通用知识推测。
回答使用中文，尽量简洁准确。
如果答案来自多个参考片段，需要综合回答，并在关键句后标注参考编号。
```

用户问题作为 user message 传入，不拼进 system 规则中。参考内容作为 system message 的上下文或单独 human message 的上下文均可，后续 Spec 再根据 LangChain 当前推荐 API 固定实现方式。

#### 结果返回与会话记录

基础响应建议包含：

```json
{
  "answer": "代码提交需要先通过本地测试，并遵守分支与提交信息规范。[参考1]",
  "sources": [
    {
      "document_id": 1,
      "document_name": "研发规范.md",
      "kb_id": 2,
      "chunk_id": 10,
      "chunk_index": 3,
      "page_number": null,
      "section_title": "代码提交",
      "score": 0.82
    }
  ],
  "hit_count": 1,
  "latency_ms": 820
}
```

当前模型中已有 `ChatSession` 和 `ChatMessage`，但多轮对话是第 18 章范围。基础阶段可以选择只返回结果、不落会话；如果请求传入有效 `session_id`，也可以记录 user / assistant 两条消息。为控制范围，本 Plan 推荐基础查询先不强制实现多轮记忆，只在 Spec 中明确是否写入当前已有聊天表。

### 1.2.3 关键分支与边界条件

#### 分支一：正常命中并生成回答

用户对有读权限知识库提问，向量检索召回有效 chunk，模型基于参考内容生成答案。响应中必须包含 answer 和 sources。日志至少记录 `user_id`、`kb_ids`、`hit_count`、`embedding_elapsed_ms`、`retrieval_elapsed_ms`、`generation_elapsed_ms` 和总耗时。

#### 分支二：无检索结果

如果没有召回任何 chunk，服务直接返回固定拒答文案，不调用聊天模型。这样可以节省模型成本，也减少模型在空上下文下发挥通用知识的机会。

#### 分支三：低置信度结果

当前阶段不启用低置信度阈值过滤。即使命中分数低于 `rag_min_score`，候选也可以进入 `SourceBuilder`；是否拒答由“无召回”与模型是否能基于参考内容回答共同决定。低置信度过滤后续单独规划。

#### 分支四：部分知识库无权限

只要请求中的某个 `kb_id` 无读权限，入口直接拒绝。本阶段不做“过滤无权限知识库后继续查询”的降级，以免用户误解查询范围，也避免通过响应差异探测知识库存在性。

#### 分支五：索引中的文档

当前检索 SQL 同时要求 `doc_version = kb_document.version` 和 `kb_document.status = DONE`。这能避免暴露半成品 chunk。文档替换或强制重建期间，索引管道会保持已发布文档 `status = DONE` 和旧 `version`，把重建进度写入 `kb_index_task`，因此查询继续使用旧版本 chunk；新版本完整发布后再切换 `document.version`。

#### 分支六：外部模型失败

Embedding 失败时查询无法继续，应返回服务不可用类错误并记录异常。Chat 模型失败时可以返回“当前无法生成回答，请稍后重试”，但不能返回只有检索片段拼接的伪答案。是否暴露 sources 给用户由 Spec 决定。

## 1.3 业务边界

### In Scope

本阶段必须覆盖的业务范围：

- 新增基础 RAG 查询入口的业务计划：请求携带 `question` 和 `kb_ids`，返回 answer 和 sources。
- 明确查询前读权限硬校验，所有传入知识库都必须具备读权限。
- 明确使用 `EmbeddingService.embed_query(...)` 完成问题向量化。
- 明确基础检索只使用 PGVector 向量相似度，不引入全文检索和 RRF。
- 明确检索 SQL 必须过滤允许的 `kb_id`、当前文档版本、未删除文档和已完成索引文档。
- 明确无结果时拒答，不能调用模型自由生成。
- 明确 Prompt 只允许基于参考内容回答，并要求中文、简洁和禁止编造。
- 明确 sources 至少包含 `document_id`、`document_name`、`kb_id`、`chunk_id`、`page_number` 或 `section_title`。
- 明确基础可观测信息：检索耗时、生成耗时、实际注入 Prompt 的引用数量和模型失败日志。
- 对齐当前 Python 项目的 `EmbeddingService`、`PermissionService`、`DocChunk`、`KbDocument`、`Settings` 和 `ChatOpenAI` 客户端。

### Out of Scope

本阶段明确不实现或不展开：

- 不实现 PostgreSQL 全文检索、向量 + 全文混合召回和 RRF 融合；这些属于第 13 章。
- 不实现 HyDE、多路查询、查询改写；这些属于第 14 章。
- 不实现 Reranker 精排和超时降级；这些属于第 15 章。
- 不实现精确 Token 预算裁剪；这些属于第 16 章。
- 不实现完整引用溯源、防幻觉校验模型或答案事实性二次检查；这些属于第 17 章。
- 不实现 SSE 流式输出、多轮对话记忆和会话标题生成；这些属于第 18 章。
- 不新增前端页面。
- 不改索引管道、分块策略、Embedding 缓存 key 或文档重建流程。
- 不引入新的任务队列、缓存层或检索引擎。
- 不做多租户字段改造；本阶段沿用现有 `kb_id` 权限隔离，未来租户字段补齐后查询 SQL 必须同步增加 `tenant_id` 过滤。

## 1.4 方案取舍

### 推荐方案：基础向量查询服务显式编排

新增 `RagQueryService` 显式串联权限校验后的向量化、检索、上下文组装、Prompt 调用和响应构建。检索层新增专用 Repository 方法直接写 SQLAlchemy / SQL，保证权限、版本和状态过滤清晰可测。

优点：

- 最贴合当前 AGENTS.md 对服务层边界、权限硬过滤和可测试性的要求。
- 后续可以平滑插入混合检索、RRF、Reranker 和上下文裁剪模块。
- 每个阶段都能独立测试，避免把核心规则藏在 Prompt 或一条 Chain 里。

代价：

- 初始代码比一行式 LangChain RetrievalQA 多一些。
- 需要自己定义 hit DTO、source builder 和拒答策略。

### 备选方案一：直接使用 LangChain Retriever / Chain

把 PGVector 封装成 LangChain retriever，再接入通用 RAG Chain。

优点是代码量少，能更快演示。缺点是权限过滤、当前版本过滤、拒答策略、引用结构和日志指标容易分散或被抽象遮住，不适合作为本项目企业级查询主链路。

### 备选方案二：一步到位实现混合检索

在基础阶段同时做向量检索、全文检索、RRF 和 Reranker。

优点是召回质量更接近最终形态。缺点是会把第 13-15 章的复杂度提前塞进本阶段，导致基础生成、权限、拒答和引用这些底线没有被单独验证。

本 Plan 推荐采用“基础向量查询服务显式编排”。先把查询闭环跑稳，再按参考资料顺序叠加增强能力。

## 1.5 与当前代码的衔接计划

后续 Spec 可围绕以下模块拆分：

```plain
app/api/routes/rag.py
  -> 新增 POST /query 查询入口

app/schemas/rag.py
  -> 定义 RagQueryRequest、RagQueryResponse、SourceCitation

app/services/rag_query.py
  -> 编排向量化、检索、拒答、Prompt 生成和响应构建

app/repositories/chunks.py
  -> 增加 search_by_vector(...)，返回带 score 和文档名的检索结果

app/services/source_builder.py
  -> 可选；把 chunk hit 转换为上下文文本和 sources

app/api/router.py
  -> include rag router，prefix="/rag"
```

当前 `ChunkRepository` 只有 `insert_many(...)`、`delete_older_versions(...)` 和 `delete_by_doc_id(...)`，因此基础查询实现必须补齐读方法。建议 Repository 返回轻量对象，而不是直接把 ORM 对象暴露到服务层：

```plain
ChunkSearchHit
  -> chunk_id
  -> doc_id
  -> document_name
  -> kb_id
  -> chunk_index
  -> content
  -> page_num
  -> section_title
  -> score
```

聊天模型调用建议复用 `get_chat_model()` 返回的 `ChatOpenAI` 客户端。具体 LangChain 调用方式、消息类型和异步 API 在 Spec 或实现阶段必须查阅当前版本文档或源码后固定，避免凭记忆写过期 API。

## 1.6 验收方向

本 Plan 不直接修改代码，但后续 Spec 和实现应能验证以下行为：

- 有读权限用户可以对一个或多个知识库发起基础 RAG 查询。
- 无读权限用户无法查询目标知识库，且不会触发向量化、检索或模型调用。
- 检索 SQL 包含 `kb_id` 过滤、当前版本过滤、文档未删除过滤和文档 `DONE` 状态过滤。
- 查询问题会调用与建库一致的 Embedding 模型维度。
- 无召回结果时返回“在知识库中未找到相关内容”，不调用聊天模型。
- 当前阶段不按 `rag_min_score` 过滤候选；后续如启用阈值，需要补充独立验收标准。
- 正常命中时，Prompt 中只包含允许知识库的参考内容。
- 正常响应包含 answer、sources、hit_count 和 latency_ms，其中 `hit_count` 与实际返回的 sources 数量一致。
- sources 至少能追溯到文档、知识库、chunk、页码或章节。
- 日志能区分 embedding、retrieval 和 generation 耗时。
- 本文档文件名符合 Plan 阶段要求，已使用 `-plan.md` 后缀。

## 1.7 下一步文档建议

完成本 Plan 后，下一份 Spec 应重点回答：

- `POST /api/v1/rag/query` 的请求体、响应体、错误码和权限依赖。
- `RagQueryService` 的方法签名、输入校验、异常边界和拒答文案。
- `ChunkRepository.search_by_vector(...)` 的 SQLAlchemy / PGVector 查询写法、分数计算和返回 DTO。
- Prompt 模板的最终文本、上下文格式和引用编号格式。
- 是否在基础阶段写入 `ChatSession` / `ChatMessage`；如果写入，如何处理无 `session_id` 查询。
- 需要新增哪些单元测试和 API 测试，尤其是权限过滤、当前版本过滤、低召回拒答和引用元数据。
