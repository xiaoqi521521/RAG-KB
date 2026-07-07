# 12-基础 RAG 查询管道——检索与生成 Process

本文基于 [12-基础 RAG 查询管道——检索与生成-spec.md](/D:/Code/python/Practical_Project/rag-kb/docs/specs/12-基础%20RAG%20查询管道——检索与生成-spec.md) 的技术方案，记录基础 RAG 查询管道的实际代码落地过程。

## 3.1 实际文件变更清单

### 新增文件

- `app/schemas/rag.py`：定义 `RagQueryRequest`、`SourceCitation`、`RagQueryResponse`，负责基础查询接口输入输出结构。
- `app/services/source_builder.py`：定义 `SourceBuilder`，负责将检索命中转换为 Prompt 上下文和结构化引用来源。
- `app/services/rag_query.py`：定义 `RagQueryService`，编排问题向量化、向量检索、低召回拒答、上下文组装和 ChatOpenAI 生成。
- `app/api/routes/rag.py`：新增 `POST /api/v1/rag/query` 路由，完成读权限硬校验并调用查询服务。
- `tests/services/test_source_builder.py`：覆盖引用编号、TopN 截断和上下文字符预算。
- `tests/services/test_rag_query.py`：覆盖无召回拒答、低分候选仍可进入上下文、正常生成和空模型响应异常。
- `tests/repositories/test_chunks.py`：覆盖向量检索 SQL 的权限范围、当前版本、完成状态和未删除过滤。
- `tests/api/test_rag.py`：覆盖查询接口的读权限校验、知识库 ID 去重和权限失败短路。
- `docs/plans/12-基础 RAG 查询管道——检索与生成-plan.md`：记录基础 RAG 查询管道的业务流程、范围边界和实施计划。
- `docs/specs/12-基础 RAG 查询管道——检索与生成-spec.md`：记录接口模型、代码执行流程、返回数量语义和验收标准。
- `docs/process/12-基础 RAG 查询管道——检索与生成-process.md`：记录实际落地文件、核心代码和验证结果。

### 修改文件

- `app/repositories/chunks.py`：新增 `ChunkSearchHit` 和 `search_by_vector(...)`，支持 PGVector `<=>` 距离排序，并过滤当前文档版本、完成状态和未删除文档。
- `app/api/router.py`：挂载 `rag.router` 到 `/api/v1/rag`。
- `pyproject.toml`：为 pytest 增加 `pythonpath = ["."]`，使直接运行子目录测试时能稳定导入 `app` 包。
- `docs/specs/12-基础 RAG 查询管道——检索与生成-spec.md`：同步当前阶段不按 `rag_min_score` 过滤，明确 `RAG_RETURN_TOP_N` 控制最多进入 Prompt 和 sources 的 chunk 数量。
- `docs/process/12-基础 RAG 查询管道——检索与生成-process.md`：同步最终实现、返回数量逻辑和验证结果。

### 删除文件

无。

### 关键 Diff

- 新增 `POST /api/v1/rag/query` 路由，路由层先逐个校验知识库读权限，再调用 RAG 查询服务。
- 新增 `ChunkRepository.search_by_vector(...)`，使用 PGVector `<=>` 距离排序，并过滤当前文档版本、DONE 状态和未删除文档。
- 新增 `RagQueryService`，串联问题向量化、向量检索、Prompt 组装和 ChatOpenAI 异步生成。
- 新增 `SourceBuilder`，统一生成 `[参考N]` 上下文和结构化 `sources`。
- 增加服务、Repository 和 API 测试，覆盖权限短路、低分候选进入上下文、引用构建和 PGVector SQL 条件。

### 关联文档同步

- 新增 Plan：`docs/plans/12-基础 RAG 查询管道——检索与生成-plan.md`。
- 新增并同步 Spec：`docs/specs/12-基础 RAG 查询管道——检索与生成-spec.md`。
- 新增并同步本文档：`docs/process/12-基础 RAG 查询管道——检索与生成-process.md`。
- 实现结果与当前 Spec 保持一致：基础阶段不写会话表，不做混合检索、RRF、Reranker、SSE 或多轮对话。

## 3.2 实际代码执行流程

### 实际总体执行链路

```plain
POST /api/v1/rag/query
  -> RagQueryRequest 校验并清理 question、kb_ids
  -> 路由层逐个调用 PermissionService.require_read(kb_id, user)
  -> RagQueryService.query(...)
  -> EmbeddingService.embed_query(question)
  -> ChunkRepository.search_by_vector(query_vector, kb_ids, top_k)
  -> 无命中时返回固定拒答
  -> SourceBuilder.build(hits, return_top_n)
  -> ChatOpenAI.ainvoke([SystemMessage, HumanMessage])
  -> 返回 ApiResponse[RagQueryResponse]
```

### 完整落地流程

1. `RagQueryRequest` 在 `app/schemas/rag.py` 中完成请求体校验：
   - `question` 去除首尾空白，空白字符串会被拒绝。
   - `kb_ids` 保留首次出现顺序并去重，非正数会被拒绝。
   - `session_id` 保留字段但本阶段不使用。

2. `app/api/routes/rag.py` 的 `query_rag(...)` 在调用服务前逐个校验读权限：
   - 任何一个 `kb_id` 无权限时，直接抛出权限异常。
   - 权限失败后不会调用 Embedding、Repository 或 Chat 模型。

3. `RagQueryService.query(...)` 负责基础 RAG 主流程：
   - 调用 `EmbeddingService.embed_query(...)` 生成查询向量。
   - 调用 `ChunkRepository.search_by_vector(...)` 在允许知识库范围内召回 chunk。
   - 当前阶段不按 `settings.rag_min_score` 过滤候选。
   - 无命中时返回固定拒答。
   - 命中结果后交给 `SourceBuilder` 生成上下文和 sources。
   - 使用 `SystemMessage` 和 `HumanMessage` 调用 `chat_model.ainvoke(...)`。

4. `ChunkRepository.search_by_vector(...)` 使用 SQLAlchemy 表达式生成 PGVector 距离查询：
   - `DocChunk.kb_id.in_(kb_ids)` 限制允许知识库。
   - `DocChunk.doc_version == KbDocument.version` 只召回当前完成版本。
   - `KbDocument.status == DONE` 和 `KbDocument.is_deleted.is_(False)` 排除未完成、失败和已删除文档。
   - `score = 1 / (1 + distance)` 统一为服务层可比较分数。
   - `<=>` 距离表达式显式使用 `type_coerce(..., Float)` 标记为浮点数，避免 pgvector 结果处理器把距离值当向量解析。

5. `SourceBuilder.build(...)` 控制进入 Prompt 的内容：
   - 先按 `rag_return_top_n` 截断命中数量。
   - 再按 `settings.rag_context_max_tokens * 4` 的近似字符预算裁剪上下文。
   - sources 顺序与 `[参考N]` 编号一致。

6. 异常处理：
   - Embedding 失败返回 503。
   - 检索 SQLAlchemy 异常返回 500。
   - Chat 模型调用失败或返回空内容返回 503。
   - 不返回仅由 chunk 拼接成的伪答案。

## 3.3 核心代码片段及讲解

### 权限入口

```python
for kb_id in request.kb_ids:
    await permission_service.require_read(kb_id, user)
```

读权限在路由层、向量化之前完成。这保证无权限知识库不会进入 embedding、SQL 检索或 Prompt 组装链路，符合“权限隔离必须在检索层硬过滤，不能依赖 Prompt”的项目约束。

### 当前版本向量检索

```python
distance_expr = type_coerce(DocChunk.embedding.op("<=>")(query_vector), Float).label("distance")
statement = (
    select(...)
    .join(KbDocument, DocChunk.doc_id == KbDocument.id)
    .where(
        DocChunk.kb_id.in_(kb_ids),
        DocChunk.doc_version == KbDocument.version,
        KbDocument.status == DocumentStatus.DONE.value,
        KbDocument.is_deleted.is_(False),
    )
    .order_by(distance_expr)
    .limit(top_k)
)
```

这里没有把多个知识库拆成多次查询再在 Python 中截断，而是在同一条 SQL 中对允许的 `kb_ids` 做全局排序。`doc_version = document.version` 能避免新版本 chunk 未完成前被召回，`KbDocument.status == DONE` 能过滤未发布文档；文档替换或强制重建期间，索引管道保持已发布文档 `DONE` 和旧版本号，因此查询继续召回旧版本 chunk。`type_coerce(..., Float)` 是 pgvector + SQLAlchemy 的关键边界：`DocChunk.embedding.op("<=>")(...)` 默认会继承左侧 Vector 类型，如果不显式改成浮点，数据库返回的距离值会被 pgvector 的向量解析器误处理。

### 无召回拒答

```python
if not hits:
    return self._refusal_response(started_at)
```

无命中时不会调用聊天模型。当前阶段暂不使用 `rag_min_score` 过滤候选，低分 chunk 仍可进入 `SourceBuilder`；最终进入 Prompt 和响应 `sources` 的数量由 `.env` 中的 `RAG_RETURN_TOP_N` 和上下文预算控制。相似度阈值过滤后续单独规划。

### ChatOpenAI 异步调用

```python
messages = [
    SystemMessage(content=system_prompt),
    HumanMessage(content=question),
]
response = await self.chat_model.ainvoke(messages)
```

实现按 Context7 查到的 LangChain 官方消息调用方式落地，并使用异步 `ainvoke(...)` 适配 FastAPI async 请求链路。本阶段没有使用 LangChain 通用 RAG Chain，避免权限过滤、拒答和 sources 结构被抽象隐藏。

## 3.4 验证结果

已执行以下命令：

```powershell
uv run pytest tests/services/test_rag_query.py::test_query_passes_all_hits_to_source_builder_without_similarity_threshold -v
```

结果：1 passed，确认低于 `rag_min_score` 的候选 chunk 当前仍会进入 `SourceBuilder`，最终返回数量由 `RAG_RETURN_TOP_N` 控制。

```powershell
uv run pytest tests/services/test_source_builder.py tests/services/test_rag_query.py tests/repositories/test_chunks.py tests/api/test_rag.py -v
```

结果：11 passed，1 个 FastAPI TestClient 既有弃用警告。

```powershell
uv run pytest -v
```

结果：94 passed，1 个 FastAPI TestClient 既有弃用警告。

```powershell
uv run ruff check app tests
```

结果：All checks passed。
