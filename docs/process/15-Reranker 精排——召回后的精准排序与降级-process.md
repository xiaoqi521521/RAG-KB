# 15-Reranker 精排——召回后的精准排序与降级 Process

本文记录本阶段根据 `docs/specs/15-Reranker 精排——召回后的精准排序与降级-spec.md` 和 `docs/references/15-Reranker 精排——召回后的精准排序与降级.md` 的实际代码落地过程。实现范围限定为新增独立 v4 查询管道：在 HyDE 增强检索之后接入 Reranker 精排、低置信度过滤和上下文裁剪占位，并在精排不可用时回退到 RRF 候选。

本阶段按 `$find-docs` 要求使用 Context7 查询 DashScope Python SDK 文档，选择 `/dashscope/dashscope-sdk-python`。文档确认 `TextReRank.call(...)` 支持 `qwen3-rerank`，输入包含 `query`、`documents`、`top_n` 和 `return_documents`，响应包含 `output.results[].index`、`output.results[].relevance_score` 以及可选 `usage`，因此实现继续使用项目现有 `httpx` HTTP 封装，不新增 `dashscope` SDK 依赖。

## 3.1 实际文件变更清单

### 新增文件

- `app/services/reranker.py`：新增 Reranker 编排服务，负责候选截取、调用 DashScope 客户端、按 provider `index` 映射回原始 chunk、替换精排分数，以及超时、HTTP 错误、空结果和非法结果的 RRF 降级。
- `app/services/confidence_filter.py`：新增低置信度过滤器，仅用于真实精排成功后的 Reranker 分数；全部低分时保留最高分 1 条。
- `app/services/context_trimmer.py`：新增上下文裁剪占位服务，本阶段原样返回候选，预留第 16 章 token 预算裁剪替换点。
- `app/services/rag_query_v4.py`：新增独立 v4 RAG 查询管道，复用 v3 的增强检索、固定拒答、上下文构建和生成语义，在 `EnhancedRetriever` 后、`SourceBuilder` 前接入精排、过滤和裁剪占位。
- `tests/services/test_reranker.py`：覆盖精排结果映射、候选数不足跳过外部调用、超时降级、HTTP 降级、空结果降级和 index 越界降级。
- `tests/services/test_confidence_filter.py`：覆盖阈值过滤、全低分保留最高分、空候选和非正阈值不过滤。
- `tests/services/test_context_trimmer.py`：覆盖上下文裁剪占位服务原样返回候选和空列表。
- `tests/services/test_rag_query_v4.py`：覆盖 v4 管道中的精排、过滤、裁剪、降级跳过过滤、候选数不足跳过过滤、拒答和生成异常。
- `tests/integrations/test_dashscope.py`：覆盖 DashScope Reranker HTTP payload、`qwen3-rerank` 模型、`return_documents=false`、`top_n` 截断和 `usage.total_tokens` 解析。
- `docs/process/15-Reranker 精排——召回后的精准排序与降级-process.md`：记录本阶段实际实现过程和验证结果。

### 修改文件

- `app/integrations/dashscope.py`：将 `DashScopeRerankerClient.rerank(...)` 从分数列表返回改为结构化 `RerankApiResponse`，解析 provider 返回的 `index`、`relevance_score` 和 `usage.total_tokens`。
- `app/api/routes/rag.py`：依赖组装支持 `rag_query_pipeline="v4"`，注入 `DashScopeRerankerClient`、`RerankerService`、`ConfidenceFilter`、`ContextTrimmer` 和 `RagQueryServiceV4`；`v3` 不构造精排相关依赖。
- `app/core/config.py`：将 `reranker_model` 默认值更新为 `qwen3-rerank`，与本阶段阿里云 Reranker 约定一致。
- `app/schemas/rag.py`：更新 `SourceCitation.score` 注释，说明 v4 精排成功时为 Reranker 分数，精排降级时为 RRF 分数。
- `tests/api/test_rag.py`：补充 v4 依赖组装测试，以及 v3 不构造 Reranker 依赖的回归测试。
- `tests/test_initialization.py`：补充 `reranker_model == "qwen3-rerank"` 默认配置断言。
- `docs/specs/15-Reranker 精排——召回后的精准排序与降级-spec.md`：同步实现细节，明确候选数不足跳过精排时也跳过置信度过滤，因为此时分数仍是 RRF 尺度。

### 删除文件

无。

### Diff 摘要

- 新增 `rag_query_v4.py`，没有在 `rag_query_v3.py` 上改造；v3 继续表示 HyDE 增强 RAG，v4 表示 HyDE 增强 + Reranker 精排 RAG。
- Reranker 使用阿里云 `qwen3-rerank`，请求体使用 `input.query`、`input.documents`、`parameters.top_n` 和 `parameters.return_documents=false`。
- 精排成功时按 provider `index` 映射回原始 `ChunkSearchHit`，并把 `score` 替换为 `relevance_score`。
- 精排超时、HTTP 错误、空结果、非法 index、非法 score 或未知异常时，回退到 RRF 前 `top_n` 候选，不向用户暴露 5xx。
- `ConfidenceFilter` 只在真实精排成功后执行；精排降级或候选数不足跳过外部精排时都跳过过滤，避免把 RRF 分数当成 Reranker 分数。
- `ContextTrimmer` 仅占位，不读取 token 预算、不截断内容、不改变分数或引用元数据。

### 关联文档同步

- 本阶段新增当前 Process 文档。
- Spec 已同步“候选数不足跳过精排时跳过低置信度过滤”的实现语义，避免 `degraded=False` 被误解为一定产生了 Reranker 分数。
- 未修改 HTTP 请求体、响应体、数据库结构或前端页面，因此无需同步 API 契约文档。

## 3.2 实际代码执行流程

### 实际总体执行链路

1. `POST /api/v1/rag/query` 接收原有 `RagQueryRequest`。
2. 路由层逐个校验 `kb_ids` 读权限；权限失败时直接返回 403，不进入检索、Reranker、上下文构建或聊天模型。
3. 当 `rag_query_pipeline="v4"` 时，依赖组装创建 `RagQueryServiceV4`。
4. `RagQueryServiceV4.query(...)` 清理问题首尾空白，并调用 `EnhancedRetriever.retrieve(...)`。
5. `EnhancedRetriever` 继续执行第 14 章链路：原始问题混合检索 + HyDE 向量检索 + 二次 RRF。
6. `RerankerService.rerank(...)` 接收增强检索候选，使用用户原始问题和候选 chunk 正文调用 DashScope Reranker。
7. 精排成功时，服务按 provider 返回的 `index` 映射回原始候选，并按 `relevance_score` 降序返回。
8. 精排失败或结果不可用时，服务降级使用增强检索返回的 RRF 前 `reranker_top_n` 候选。
9. 真实精排成功后执行 `ConfidenceFilter.filter(...)`；降级或候选数不足跳过精排时跳过该过滤。
10. `ContextTrimmer.trim(...)` 作为占位入口原样返回候选。
11. `SourceBuilder.build(...)` 构建最终进入 Prompt 的上下文和引用来源。
12. 聊天模型基于真实 chunk 上下文生成答案，API 返回原有 `RagQueryResponse(answer, sources, hit_count, latency_ms)`。

### 完整落地流程

- `v1/v2/v3` 依赖组装和查询语义保持兼容；只有配置为 `v4` 时才构造精排相关依赖。
- DashScope 客户端只负责 provider HTTP 调用和响应解析，不处理 `ChunkSearchHit`，不做降级排序。
- Reranker 服务层统一处理降级，确保 provider 超时或异常不会中断主查询链路。
- 候选数小于等于 `reranker_top_n` 时不调用外部 Reranker，直接返回当前 RRF 候选；因为此时没有 Reranker 分数，v4 跳过低置信度过滤。
- 候选数小于等于 `reranker_top_n` 时会打印 INFO 日志：`[Reranker] 候选数不超过 topN，跳过精排：候选=...，topN=...`，便于和“未进入 v4 管道”区分。
- 低置信度过滤发生在上下文裁剪占位之前；全部低分时保留最高分 1 条，让后续 Prompt 和固定拒答策略继续约束答案。
- 上下文裁剪占位发生在 `SourceBuilder` 之前，但当前实现不做 token 预算、不截断 chunk，正式实现留到第 16 章。
- `sources[].score` 不新增 score 类型字段：精排成功时是 Reranker 分数，精排降级或跳过时是 RRF 分数。

## 3.3 核心代码片段及讲解

### DashScope Reranker 客户端

```python
payload = {
    "model": self.settings.reranker_model,
    "input": {"query": normalized_query, "documents": documents},
    "parameters": {"top_n": effective_top_n, "return_documents": False},
}
```

客户端使用现有 `httpx.AsyncClient`，`timeout` 来自 `reranker_timeout_ms`。`top_n` 在调用前截断到候选数量，`return_documents=false` 避免 provider 返回重复正文。响应解析为 `RerankApiResponse(results, total_tokens)`，其中每个结果保留 provider 返回的原始候选下标。

### RerankerService 映射与降级

```python
response = await self.client.rerank(
    query=question.strip(),
    documents=[hit.content for hit in candidates],
    top_n=effective_top_n,
)
hits = self._map_results(response.results, candidates)
```

服务层不按返回顺序猜测候选关系，而是必须通过 `result.index` 映射回 `candidates[result.index]`。映射后复制原始 chunk 的文档、知识库、页码、章节等引用元数据，只替换 `score` 为 `relevance_score`。超时、HTTP 错误、空结果、越界 index、非法 score 和未知异常都会返回 RRF 前 `top_n` 候选，并记录 `degraded_reason`。

### v4 查询管道

```python
rerank_result = await self.reranker.rerank(question=question, candidates=candidates)
hits = rerank_result.hits
if not rerank_result.degraded and rerank_result.degraded_reason != "skipped_not_enough_candidates":
    hits = self.confidence_filter.filter(hits)
return self.context_trimmer.trim(hits)
```

这里是本阶段最关键的顺序：先精排，再按精排结果取 `top_n`，再在真实精排成功时过滤低分，最后进入上下文裁剪占位。降级和候选数不足跳过精排时都保持 RRF 分数尺度，不套用 `rag_min_score`。

### ContextTrimmer 占位

```python
class ContextTrimmer:
    def trim(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        return hits
```

占位服务只提供稳定入口，不改变候选数量、顺序、内容、分数或来源元数据。第 16 章实现 token 预算裁剪时可以替换该文件或扩展该类，而不必再次改变 v4 主流程结构。

### 依赖组装

```python
if settings.rag_query_pipeline == "v4":
    return RagQueryServiceV4(
        retriever=enhanced_retriever,
        reranker=RerankerService(
            client=DashScopeRerankerClient(settings=settings),
            top_n=settings.reranker_top_n,
        ),
        confidence_filter=ConfidenceFilter(min_score=settings.rag_min_score),
        context_trimmer=ContextTrimmer(),
        source_builder=source_builder,
        chat_model=chat_model,
        settings=settings,
    )
```

路由层仍只暴露同一个 `/api/v1/rag/query` 入口，具体查询管道由 `rag_query_pipeline` 选择。`v4` 复用 v3 的 `EnhancedRetriever`，并额外注入精排、置信度过滤和上下文裁剪占位。

## 3.4 验证结果

本阶段按 TDD 推进，先运行目标测试确认缺少 `confidence_filter`、`context_trimmer`、`reranker`、`rag_query_v4` 和结构化 Rerank DTO 导致失败，再实现最小代码并补齐行为测试。

已执行目标测试：

```powershell
uv run pytest tests/services/test_confidence_filter.py tests/services/test_context_trimmer.py tests/services/test_reranker.py tests/services/test_rag_query_v4.py tests/integrations/test_dashscope.py tests/api/test_rag.py tests/test_initialization.py -v
```

结果：34 passed，保留 1 条第三方 `fastapi.testclient` / Starlette 弃用警告。

已执行 Ruff：

```powershell
uv run ruff check app tests
```

结果：All checks passed。

已执行全量测试：

```powershell
uv run pytest -q
```

结果：155 passed，保留 1 条第三方 `fastapi.testclient` / Starlette 弃用警告。

## 3.5 Review 关注点

- 请重点 review `RagQueryServiceV4` 是否满足“新增独立查询模块，不改造 `rag_query_v3.py`”的边界。
- 请确认精排顺序为“RRF 候选 -> Reranker -> 取精排 top_n -> 低置信度过滤 -> 上下文裁剪占位 -> SourceBuilder”。
- 请确认降级或候选数不足跳过精排时不执行 `ConfidenceFilter`，避免混用 RRF 分数和 Reranker 分数阈值。
- 请确认 `ContextTrimmer` 当前仅占位，真实 token 预算裁剪留到第 16 章。
- 请确认 `DashScopeRerankerClient` 没有引入 `dashscope` SDK 新依赖，且没有在日志或异常中输出 API Key 或完整 chunk 正文。
