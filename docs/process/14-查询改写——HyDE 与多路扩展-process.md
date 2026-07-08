# 14-查询改写——HyDE 与多路扩展 Process

本文记录本阶段根据 `docs/plans/14-查询改写——HyDE 与多路扩展-spec.md` 和 `docs/references/14-查询改写——HyDE 与多路扩展.md` 的实际代码落地过程。实现范围限定为查询改写增强检索：抽取公共 RRF、接入 HyDE 向量检索、提供多路查询扩展方法，并通过 `rag_query_pipeline="v3"` 接入现有 RAG 查询入口。

本阶段按 `$find-docs` 要求使用 Context7 查询 LangChain Python 文档，选择 `/websites/langchain_oss_python_langchain`。文档确认聊天模型可以接收 `SystemMessage` / `HumanMessage` 消息列表并返回带 `content` 的 `AIMessage`，因此实现沿用项目现有 `chat_model.ainvoke(messages)` 调用形态。

## 3.1 实际文件变更清单

### 新增文件

- `app/services/rrf.py`：抽取公共 RRF 融合函数，供第 13 章混合检索和第 14 章增强检索复用。
- `app/services/query_rewriter.py`：新增查询改写服务，封装 HyDE 生成、多路扩展生成、Redis 缓存、输出清洗和降级原因记录。
- `app/services/enhanced_retriever.py`：新增增强检索服务，编排原始问题混合检索、HyDE 向量检索和二次 RRF 融合。
- `app/services/rag_query_v3.py`：新增 v3 RAG 查询服务，复用 v2 的回答生成、拒答和错误语义，替换检索来源为增强检索。
- `tests/services/test_rrf.py`：覆盖公共 RRF 的单路计分、跨路去重、分数累加、稳定排序和参数校验。
- `tests/services/test_query_rewriter.py`：覆盖 HyDE 缓存命中、模型生成、缓存写入、模型失败降级、多路扩展清洗和 Redis 读失败降级。
- `tests/services/test_enhanced_retriever.py`：覆盖原始混合结果与 HyDE 向量结果二次 RRF、HyDE 不可用降级、HyDE 向量化失败降级，以及多路扩展不进入本阶段检索链路。
- `tests/services/test_rag_query_v3.py`：覆盖 v3 查询服务的固定拒答、真实 chunk 上下文生成、`hit_count` 语义和生成异常处理。
- `docs/process/14-查询改写——HyDE 与多路扩展-process.md`：记录本阶段实现过程，方便 review。

### 修改文件

- `app/services/hybrid_retriever.py`：删除文件内私有 RRF 实现，改为导入并复用 `app.services.rrf.rrf_fuse(...)`；向量 + 全文的第一阶段融合行为保持不变。
- `app/api/routes/rag.py`：依赖组装支持 `rag_query_pipeline="v3"`，新增 `QueryRewriter`、`EnhancedRetriever`、`RagQueryServiceV3` 的组合；未新增 HTTP 路由、请求字段或响应字段。
- `tests/services/test_hybrid_retriever.py`：移除“RRF 模块不存在”的旧断言，保留混合检索行为验证。
- `tests/api/test_rag.py`：补充 v3 管道依赖组装测试，并为 fake settings 增加已有配置字段 `chat_model` 和 `query_cache_ttl_seconds`。

### 删除文件

无。

### Diff 摘要

- RRF 从 `HybridRetriever` 内部私有函数抽取为公共 `rrf_fuse(...)`，避免混合检索和 HyDE 增强检索各自维护一套融合逻辑。
- 原始问题仍先走 `HybridRetriever`，也就是“向量检索 + 全文检索 + 第一阶段 RRF”。
- HyDE 生成的假设性回答只用于向量检索，不进入最终回答 Prompt，不作为引用来源。
- 第二阶段 RRF 只融合两路结果：`original_hybrid` 和 `hyde_vector`。
- 多路扩展已实现 `expand_queries(...)`、缓存、清洗和测试，但没有接入 `EnhancedRetriever.retrieve(...)`。
- 未新增 `rag_hyde_enabled`、`rag_multi_query_enabled` 等配置项；本阶段复用已有 `query_cache_ttl_seconds`、`chat_model`、`rag_rrf_k`、`rag_vector_top_k`、`rag_return_top_n`。

### 关联文档同步

- 本阶段新增当前 Process 文档。
- Spec 中“无新增配置”“先原始混合 RRF，再与 HyDE 向量结果二次 RRF”“多路扩展本阶段只实现方法”的边界已在代码中落地。

## 3.2 实际代码执行流程

### 实际总体执行链路

1. `POST /api/v1/rag/query` 接收原有 `RagQueryRequest`。
2. 路由层逐个校验 `kb_ids` 读权限；权限失败时直接返回 403，不进入查询改写、检索、Embedding 或聊天模型。
3. 当 `rag_query_pipeline="v3"` 时，依赖组装创建 `RagQueryServiceV3`。
4. `RagQueryServiceV3.query(...)` 清理问题首尾空白，并调用 `EnhancedRetriever.retrieve(...)`。
5. `EnhancedRetriever` 先调用 `HybridRetriever.retrieve(...)`，对原始问题执行向量召回、全文召回和第一阶段 RRF。
6. `QueryRewriter.generate_hyde_answer(...)` 生成或读取 HyDE 假设性回答。
7. HyDE 可用时，`EmbeddingService.embed_query(...)` 对 HyDE 文本向量化。
8. `ChunkRepository.search_by_vector(...)` 在已授权 `kb_ids` 内执行 HyDE 向量召回。
9. `rrf_fuse(...)` 对 `original_hybrid` 和 `hyde_vector` 做第二阶段 RRF 融合，并按 `chunk_id` 去重。
10. `SourceBuilder.build(...)` 只接收真实召回的 chunk，裁出最终进入 Prompt 的上下文和引用。
11. `chat_model.ainvoke([SystemMessage, HumanMessage])` 基于真实 chunk 上下文生成回答。
12. API 返回原有 `RagQueryResponse(answer, sources, hit_count, latency_ms)`。

### 完整落地流程

- v3 入口不改变接口契约；请求体和响应体继续沿用 v1/v2。
- 原始问题检索失败属于主链路失败，保持 v2 的 HTTP 错误语义。
- HyDE 生成失败、Redis 缓存失败、HyDE 向量化失败或 HyDE 向量检索失败只降级 HyDE 分支，最终回退到原始问题混合检索结果。
- 多路扩展方法可以被单独调用和测试，但 `EnhancedRetriever.retrieve(...)` 不调用 `expand_queries(...)`。
- 缓存 key 仅基于标准化后的问题、改写策略、模型名和版本，不包含 `kb_ids`；权限隔离仍由后续检索 SQL 的 `kb_ids` 硬过滤保证。
- 最终 `sources` 只来自 `SourceBuilder` 消费的真实 chunk；HyDE 文本和扩展查询不会进入响应或引用。

## 3.3 核心代码片段及讲解

### 公共 RRF 融合

```python
def rrf_fuse(
    ranked_results: Mapping[str, Sequence[ChunkSearchHit]],
    *,
    rrf_k: int,
) -> list[RrfSearchHit]:
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive")

    for source_name, hits in ranked_results.items():
        for rank, hit in enumerate(hits, start=1):
            scores_by_id[chunk_id] = scores_by_id.get(chunk_id, 0.0) + (1.0 / (rrf_k + rank))
```

RRF 只使用各通道内部排名，不直接混合向量相似度、全文 `ts_rank` 或 HyDE 原始分。同一 `chunk_id` 多路命中时分数累加，命中元数据保留首次出现的 `ChunkSearchHit`，同分时按首次出现顺序稳定排序。

### HyDE 与多路查询改写

```python
response = await self.chat_model.ainvoke(
    [
        SystemMessage(content="你是企业知识库查询改写助手，只输出改写结果。"),
        HumanMessage(content=prompt),
    ]
)
content = getattr(response, "content", None)
```

`QueryRewriter` 使用 LangChain 消息列表调用聊天模型，输出只作为检索改写材料。HyDE 返回单段文本，多路扩展按行或 JSON 列表清洗，去掉编号、空行、重复项和与原始问题相同的条目。Redis 读写失败只追加 `*_cache_*_failed` 降级原因，不中断查询。

### 增强检索二次融合

```python
original_result = await self.hybrid_retriever.retrieve(question=normalized_question, kb_ids=kb_ids)
hyde_hits: list[ChunkSearchHit] = []
degraded_reasons = list(await self._retrieve_hyde_hits(normalized_question, kb_ids, hyde_hits))

fused_hits = rrf_fuse(
    {
        "original_hybrid": original_result.hits,
        "hyde_vector": hyde_hits,
    },
    rrf_k=self.rrf_k,
)
```

这里与参考文献对齐为两阶段融合：原始问题先完成混合检索内部 RRF，HyDE 只做向量召回，然后再将这两路已排序结果做第二阶段 RRF。HyDE 分支失败时 `hyde_hits=[]`，公共 RRF 会自然返回原始混合结果的排序版本。

### v3 查询服务

```python
context, sources = self.source_builder.build(
    hits,
    return_top_n=self.settings.rag_return_top_n,
)
answer = await self._generate_answer(normalized_question, context, user, kb_ids)
```

`RagQueryServiceV3` 不把 HyDE 文本或扩展查询写入 Prompt。回答生成仍只依赖 `SourceBuilder` 组装出的真实 chunk 上下文；无候选时返回固定拒答并跳过聊天模型，保持防幻觉边界。

### 依赖组装

```python
if settings.rag_query_pipeline == "v3":
    return RagQueryServiceV3(
        retriever=EnhancedRetriever(
            query_rewriter=QueryRewriter(...),
            hybrid_retriever=hybrid_retriever,
            embedding_service=embedding_service,
            chunk_repository=chunk_repository,
            rrf_k=settings.rag_rrf_k,
            hyde_vector_top_k=settings.rag_vector_top_k,
        ),
        source_builder=source_builder,
        chat_model=chat_model,
        settings=settings,
    )
```

路由层只增加 v3 分支，不新增配置项。`rag_vector_top_k` 同时作为 HyDE 向量召回数量，`query_cache_ttl_seconds` 作为改写缓存 TTL，避免为单阶段能力增加额外开关。

## 3.4 验证结果

本阶段按 TDD 推进，先运行目标测试确认缺少 `rrf`、`query_rewriter`、`enhanced_retriever`、`rag_query_v3` 等模块导致失败，再实现最小代码并补齐行为测试。

已执行目标测试：

```powershell
uv run pytest tests/services/test_rrf.py tests/services/test_hybrid_retriever.py tests/services/test_query_rewriter.py tests/services/test_enhanced_retriever.py tests/services/test_rag_query_v3.py tests/api/test_rag.py -q
```

结果：24 passed，保留 1 条第三方弃用警告。

已执行全量测试：

```powershell
uv run pytest -q
```

结果：133 passed，保留 1 条第三方弃用警告。

已执行 Ruff：

```powershell
uv run ruff check .
```

结果：All checks passed。

已执行 mypy：

```powershell
uv run mypy app
```

结果：未通过，共 13 个错误；错误集中在既有项目问题，包括 `app/core/config.py` 的 `Settings` 构造类型、`jose` / `passlib` 缺少类型声明、`app/repositories/chunks.py` 的 `ts_config` 类型、异常处理器签名、Markdown 解析 nullable match，以及 `app/core/clients.py` 中 `ChatOpenAI` 参数类型。上述问题不是本阶段新增配置或查询改写链路引入。

## 3.5 Review 关注点

- 请重点 review `EnhancedRetriever.retrieve(...)` 的融合顺序是否符合预期：先原始问题混合检索，再与 HyDE 向量检索结果二次 RRF。
- 请确认 `QueryRewriter.expand_queries(...)` 只作为后续阶段准备，没有被当前 v3 主链路调用。
- 请确认 `QueryRewriter` 的缓存 key 不包含 `kb_ids` 可接受；当前设计依据是改写不读取知识库内容，权限仍在检索层硬过滤。
- 请确认 v3 不暴露 HyDE 文本、扩展查询或调试字段，保持 API 响应兼容。
