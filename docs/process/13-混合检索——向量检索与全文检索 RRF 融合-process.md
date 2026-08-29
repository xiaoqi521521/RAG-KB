# 13-混合检索——向量检索与全文检索 RRF 融合 Process

本文记录本阶段根据 `docs/specs/13-混合检索——向量检索与全文检索 RRF 融合-spec.md` 的实际代码落地过程。实现范围限定为在线查询管道的检索层升级：向量检索、PostgreSQL 全文检索、RRF 融合，以及路由层无感接入。

## 3.1 实际文件变更清单

### 新增文件

- `app/services/ts_query_builder.py`：构建 PostgreSQL 全文检索查询文本，处理停用词、常见中英文标点和精确关键词保留。
- `app/services/hybrid_retriever.py`：内聚 RRF 融合算法，按 `chunk_id` 去重并累加多路排名分，不再单独保留 `rrf.py`。
- `app/services/hybrid_retriever.py`：编排向量召回、全文召回和 RRF 融合，返回内部统计与最终候选列表。
- `tests/services/test_ts_query_builder.py`：覆盖全文查询词构建。
- `tests/services/test_hybrid_retriever.py`：覆盖 RRF 单路计分、重复 chunk 累加、同分稳定排序和混合检索编排。
- `tests/services/test_hybrid_retriever.py`：覆盖混合检索编排、全文空查询降级和低分向量候选仍参与 RRF。
- `docs/process/13-混合检索——向量检索与全文检索 RRF 融合-process.md`：记录本阶段实现过程。

### 修改文件

- `app/repositories/chunks.py`：新增 `search_by_fulltext(...)`，使用 PostgreSQL `to_tsquery`、`ts_rank`、`@@` 匹配 `content_tsv` 完成全文召回，并复用与向量检索一致的知识库、版本、状态和删除过滤。
- `app/services/indexing.py`：删除 Python 侧 `content_tsv=to_tsvector(...)` 显式赋值；索引入库只写 `content`，由数据库触发器维护 `content_tsv`。
- `app/services/ts_query_builder.py`：查询词输出改为参考文献中的 `关键词 & 关键词` 形式，供 `to_tsquery('simple', ...)` 使用。
- `app/services/rag_query.py`：保留基础向量 RAG 查询管道，继续执行向量检索；当前阶段不按 `rag_min_score` 过滤。
- `app/services/rag_query_v2.py`：新增混合检索 RAG 查询管道，依赖 `HybridRetriever`，服务层不再对 RRF 分数应用 `rag_min_score`。
- `app/api/routes/rag.py`：仅修改 `get_rag_query_service(...)` 内部依赖组装，通过 `rag_query_pipeline` 选择 v1/v2，路由函数、路径、请求体和响应体保持不变。
- `app/schemas/rag.py`：仅同步文档注释，说明 `sources[].score` 为最终排序分、混合检索阶段为 RRF 分数；未新增响应字段。
- `app/core/config.py`：新增 `rag_rrf_k: int = 60`。
- `tests/repositories/test_chunks.py`：新增全文检索 SQL 约束测试。
- `tests/services/test_indexing.py`：补充索引入库不显式写入 `content_tsv` 的断言，确保该字段只依赖数据库触发器维护。
- `tests/services/test_rag_query.py`：覆盖基础向量 RAG 查询管道。
- `tests/services/test_rag_query_v2.py`：覆盖混合检索 RAG 查询管道，验证 RRF 分数不再被服务层当作相似度阈值过滤。

### 删除文件

无。

### Diff 摘要

- 查询入口仍为 `POST /api/v1/rag/query`，`query_rag(...)` 未改变，路由层通过 `rag_query_pipeline` 在基础管道和混合检索管道之间切换。
- `HybridRetriever` 先执行向量召回，再按 `TsQueryBuilder` 结果选择是否执行全文召回，最后用 RRF 融合。
- 当前阶段不使用 `rag_min_score` 过滤候选；向量候选和全文候选都参与 RRF，最终返回数量由 `RAG_RETURN_TOP_N` 和上下文预算控制。
- `hit_count` 返回实际进入 Prompt 的引用 chunk 数量，与 `sources.length` 一致；未新增 `retrieved_count`。
- 全文检索严格使用 `DocChunk.content_tsv`，索引入库时 Python 只写 `content`；数据库触发器同步写入 `to_tsvector('simple', NEW.content)`，与参考文献 `content_tsv @@ to_tsquery(...)` 对齐。

### 关联文档同步

- 本阶段新增当前 Process 文档。
- Spec 的接口约束未发生变化：没有新增响应字段，没有加入前端开关，没有实现 Reranker、HyDE、多路查询或流式输出。

## 3.2 实际代码执行流程

### 实际总体执行链路

1. `POST /api/v1/rag/query` 接收原有 `RagQueryRequest`。
2. 路由层逐个校验 `kb_ids` 读权限；权限失败时在进入检索和模型前短路。
3. `RagQueryServiceV2.query(...)` 清理问题首尾空白，并调用 `HybridRetriever.retrieve(...)`。
4. `HybridRetriever` 调用 `EmbeddingService.embed_query(...)` 生成查询向量。
5. `ChunkRepository.search_by_vector(...)` 按 PGVector 相似度召回向量候选。
6. `TsQueryBuilder.build(...)` 生成 `关键词 & 关键词` 形式的全文检索文本；如果结果为空，全文通道降级为空列表。
7. `ChunkRepository.search_by_fulltext(...)` 对允许访问的知识库执行 PostgreSQL 全文召回。
8. `HybridRetriever` 将向量候选和全文候选直接交给内部 `_rrf_fuse(...)`。
9. RRF 融合结果转换回 `ChunkSearchHit`，其中 `score` 替换为 RRF 分数。
10. `RagQueryServiceV2` 将融合候选交给 `SourceBuilder`，再调用聊天模型生成回答。
11. API 返回原有 `RagQueryResponse(answer, sources, hit_count, latency_ms)`。

### 完整落地流程

- 向量通道保持第 12 章的权限和版本过滤：`kb_id`、当前文档版本、文档状态为完成、未删除。
- 全文通道使用相同过滤条件，避免绕过权限或读到旧版本 chunk。
- 全文查询词为空不是异常，只跳过全文召回，继续使用向量通道结果。
- 当两路结果都没有可用候选时，服务返回固定拒答，不调用聊天模型。
- 模型生成失败、空回答、Embedding 失败和数据库检索失败沿用第 12 章的 HTTP 错误语义。
- `SourceBuilder` 不感知候选来自向量还是全文，只消费融合后的有序 `ChunkSearchHit`。

## 3.3 核心代码片段及讲解

### 全文召回

```python
ts_config = literal_column("'simple'")
query_expr = func.to_tsquery(ts_config, query_text)
rank_expr = type_coerce(func.ts_rank(DocChunk.content_tsv, query_expr), Float).label("rank")
```

这里使用 PostgreSQL 官方全文检索函数：`to_tsquery` 负责把 `关键词 & 关键词` 文本转为 `tsquery`，`DocChunk.content_tsv @@ query_expr` 判断匹配，`ts_rank(DocChunk.content_tsv, query_expr)` 给全文通道内部排序。`content_tsv` 由数据库触发器生成，不再在查询阶段动态兜底。

### content_tsv 维护

```sql
CREATE OR REPLACE FUNCTION update_chunk_tsv()
RETURNS TRIGGER AS $$
BEGIN
    NEW.content_tsv := to_tsvector('simple', NEW.content);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

写入侧不再拼接章节标题或显式调用 `to_tsvector`，只保存 chunk 正文到 `content`。数据库触发器在 `INSERT` 或 `UPDATE OF content` 时维护 `content_tsv`，查询侧继续直接使用 `content_tsv` 字段，并符合参考文献中 `c.content_tsv @@ to_tsquery('simple', :tsQuery)` 的实现方式。

### RRF 融合

```python
scores_by_id[chunk_id] = scores_by_id.get(chunk_id, 0.0) + (1.0 / (rrf_k + rank))
```

RRF 只使用排名，不混合向量相似度和全文 `ts_rank`。同一 `chunk_id` 在多路结果中出现时分数累加，并保留首次出现顺序作为同分排序依据。

### 混合检索编排

```python
fused_hits = _rrf_fuse(
    {
        "vector": vector_hits,
        "fulltext": fulltext_hits,
    },
    rrf_k=self.settings.rag_rrf_k,
)
```

当前阶段不使用 `rag_min_score` 过滤候选。融合后的 RRF 分数不是百分比相似度，不能直接套用固定相似度阈值；后续如需阈值过滤，需要单独设计向量原始分、全文 rank 和 RRF 分数的语义边界。

### 路由层无感接入

以下片段是初始 v2 混合检索落地时的核心组装示例；当前实现还会向 `HybridRetriever` 注入 `PermissionService`，详见本文件第 4 节。

```python
hybrid_retriever = HybridRetriever(
    embedding_service=embedding_service,
    chunk_repository=chunk_repository,
    ts_query_builder=TsQueryBuilder(),
    settings=settings,
    permission_service=permission_service,
)
return RagQueryServiceV2(
    retriever=hybrid_retriever,
    source_builder=SourceBuilder(max_context_chars=settings.rag_context_max_tokens * 4),
    chat_model=get_chat_model(),
    settings=settings,
)
```

路由层仅替换服务内部依赖组装，`query_rag(...)`、URL、请求体和响应体没有变化；服务端可通过 `rag_query_pipeline` 在基础管道和混合检索管道之间切换。

## 3.4 验证结果

本阶段先按 TDD 写入失败测试，确认新模块缺失导致测试收集失败；实现完成后执行以下验证：

```powershell
uv run pytest tests/services/test_ts_query_builder.py tests/repositories/test_chunks.py tests/services/test_indexing.py tests/services/test_hybrid_retriever.py tests/services/test_rag_query.py tests/services/test_rag_query_v2.py -v
```

结果：37 passed。

```powershell
uv run pytest tests/api/test_rag.py -v
```

结果：4 passed，保留 1 条 FastAPI TestClient 的第三方弃用警告。

```powershell
uv run ruff check app tests
```

结果：All checks passed。

```powershell
uv run pytest -v
```

结果：112 passed，保留 1 条 FastAPI TestClient 的第三方弃用警告。

## 4. 检索层权限过滤增量

后续根据 [检索层权限过滤 Spec](../../.scratch/hybrid-retrieval-permission-filter/spec.md)、[设计方案](../agens-output/22-检索层权限过滤设计方案.md) 和 ADR-0007，在原有混合检索边界内增加第二层权限防护。该增量不改变 route 层对多知识库请求的整体拒绝语义，也不扩展 v1 直接向量管道。

### 4.1 实际文件变更

- `app/services/permissions.py`：新增 `filter_readable_kb_ids(...)`，逐库复用 `require_read(...)`，跳过不存在或已删除知识库，权限数据源故障保持 `503`。
- `app/services/hybrid_retriever.py`：`retrieve(...)` 从 `ContextVar` 读取用户，去重并过滤请求范围；空授权范围返回 `403`，缺少上下文返回 `401`。内部 `_retrieve(...)` 只接收 `allowed_kb_ids`。
- `app/services/enhanced_retriever.py`：HyDE 的 Embedding 和向量仓储复用 `HybridRetrieveResult.allowed_kb_ids`。
- `app/api/routes/rag.py`：将请求内 `PermissionService` 注入 v2/v3/v4 的混合检索组装。
- `tests/services/test_permissions.py`、`tests/services/test_hybrid_retriever.py`、`tests/services/test_enhanced_retriever.py`：覆盖授权范围过滤、短路、审计日志和 HyDE 范围传播。

### 4.2 实际执行边界

```plain
HybridRetriever.retrieve(question, requested_kb_ids)
  -> ContextVar.CurrentUser
  -> PermissionService.filter_readable_kb_ids(...)
  -> allowed_kb_ids 为空时 403
  -> _retrieve(question, allowed_kb_ids)
     -> Embedding
     -> 向量检索和全文检索使用同一 allowed_kb_ids
     -> RRF
```

部分范围被过滤时，专用审计事件记录 `user_id` 和 `denied_kb_ids`；这些标识不进入 Prometheus/Grafana 标签，也不扩展到普通认证和权限日志。v3/v4 的 HyDE 不重新读取原始请求范围。

### 4.3 增量验证

增量实现已执行全量测试，结果为 `383 passed, 3 skipped`；`uv run ruff check .` 通过。`uv run mypy app` 的剩余错误来自既有未修改模块。
