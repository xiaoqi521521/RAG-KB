# 15-Reranker 精排——召回后的精准排序与降级 Spec

本文基于 [15-Reranker 精排——召回后的精准排序与降级-plan.md](../plans/15-Reranker%20精排——召回后的精准排序与降级-plan.md)、参考资料 [15-Reranker 精排——召回后的精准排序与降级.md](../references/15-Reranker%20精排——召回后的精准排序与降级.md)，以及 [三阶段生成.md](../prompt/三阶段生成.md) 的 Spec 阶段规则，定义 Python / FastAPI / LangChain 版本的 Reranker 精排技术方案。

本阶段只升级在线查询管道中“召回之后、上下文构建之前”的排序层：新增独立的 Reranker 查询管道模块，在第 14 章 `RagQueryServiceV3` 的 HyDE 增强检索能力之后接入 Reranker 精排，并在精排不可用时回退到 RRF 排序结果。`rag_query_v3.py` 保持不变，接口路径、请求体、响应体、权限入口、查询改写、混合检索和回答生成 Prompt 保持稳定。

## 前置自检

眼下没有完全把握的事情有三类：

- 阿里百炼 Reranker HTTP 接口的完整错误响应结构、限流响应字段和不同模型之间的请求差异，可能随官方平台版本变化。
- 当前仓库已有 `app/integrations/dashscope.py` 中的 `DashScopeRerankerClient` 只返回分数列表，无法按 `index` 安全映射回原始候选；后续实现需要改造，而不是直接复用当前返回值。
- `rag_min_score` 在前序章节一直保留但未参与过滤，本阶段要正式用于 Reranker 分数过滤；如果未来切换到不同分数分布的模型，默认阈值可能需要重新评估。

影响与处理：

- 本 Spec 按 `$find-docs` 要求核对 DashScope Python SDK 文档：`TextReRank.call(...)` 支持 `model`、`query`、`documents`、`top_n`、`return_documents` 和单次 `timeout`；响应结果包含 `output.results[].index` 和 `output.results[].relevance_score`。
- 同时补查阿里云官方 Model Studio 文档：`qwen3-rerank` 走 DashScope TextReRank 同一类请求语义，使用 `input.query`、`input.documents`、`parameters.top_n`、`parameters.return_documents`；响应包含 `output.results[].index`、`output.results[].relevance_score` 和 `usage.total_tokens`。本项目本阶段明确使用阿里云 `RERANKER_MODEL=qwen3-rerank`；`gte-rerank-v2` 仅作为历史兼容模型说明，不作为默认方案。
- 由于项目当前未依赖 `dashscope` SDK，且 `httpx` 已存在，本阶段继续采用 HTTP 客户端封装，不新增 SDK 依赖。若后续切换 SDK，需要同步更新本 Spec 或 Process。

## 2.1 任务背景

第 13 章已经完成向量检索、全文检索和 RRF 融合，第 14 章已经在原始问题混合检索外增加 HyDE 向量检索和二次 RRF。当前 v3 查询链路可以扩大召回覆盖面，但 RRF 仍只基于各通道排名融合，不能像 Cross-Encoder 一样同时观察“用户问题 + 候选段落”的细粒度交互。

本阶段不在 `rag_query_v3.py` 上直接改造，而是新增 `rag_query_v4.py` 作为 Reranker 精排查询管道。这样 v3 保持为“HyDE 增强 RAG”，v4 明确表示“HyDE 增强 + Reranker 精排 RAG”，便于灰度、回滚、横向评估和后续 Process 追溯。

本阶段目标是在 `EnhancedRetriever` 输出候选后加入 Reranker 精排：

```plain
用户问题
  -> 权限校验
  -> 查询改写 + 混合检索 + 二次 RRF
  -> Reranker 精排
  -> 低置信度过滤
  -> 上下文裁剪占位
  -> SourceBuilder 构建上下文和引用
  -> Chat 模型基于真实 chunk 生成回答
```

技术前提：

- `app/services/enhanced_retriever.py` 已返回按二次 RRF 排序的 `ChunkSearchHit` 列表。
- `app/services/rag_query_v3.py` 已负责 v3 查询主流程、固定拒答、`SourceBuilder` 和聊天模型调用；本阶段只参考其流程，不直接修改该文件。
- `app/core/config.py` 已有 `reranker_endpoint`、`reranker_model`、`reranker_timeout_ms`、`reranker_top_n` 和 `rag_min_score`；部署环境必须设置 `RERANKER_MODEL=qwen3-rerank`。
- `app/integrations/dashscope.py` 已有 `DashScopeRerankerClient` 雏形，但需要改造返回结构和错误语义。
- 路由层已在向量化、检索、精排和生成之前完成知识库读权限硬校验。

核心目标：

- 精排只消费前序已授权候选，不新增召回范围。
- 精排成功时按 `relevance_score` 重排，并只保留 `reranker_top_n` 个候选。
- 精排超时、异常、空返回或结果无法映射时，回退到 RRF 排序结果。
- 使用 Reranker 分数执行低置信度过滤，减少弱相关 chunk 进入 Prompt。
- 保留 chunk 的引用元数据，响应字段保持兼容。

## 2.2 范围对齐

### In Scope

本阶段技术交付物：

- 改造 `app/integrations/dashscope.py`，让 Reranker 客户端返回带 `index`、`relevance_score` 和 `total_tokens` 的结构化结果。
- 新增 `app/services/reranker.py`，封装候选截取、外部精排调用、结果映射、排序和 RRF 降级。
- 新增 `app/services/confidence_filter.py`，按 `settings.rag_min_score` 过滤低分候选，并在全部低分时保留最高分 1 条供后续生成侧拒答规则判断。
- 新增 `app/services/context_trimmer.py`，提供上下文裁剪占位服务，本阶段只原样返回候选，第 16 章再替换为 token 预算裁剪实现。
- 新增 `app/services/rag_query_v4.py`，作为独立 Reranker 查询管道，把 Reranker、置信度过滤和上下文裁剪占位接在 `EnhancedRetriever` 之后、`SourceBuilder` 之前。
- 修改 `app/api/routes/rag.py`，新增 `rag_query_pipeline="v4"` 依赖组装，`v3` 组装保持 HyDE 增强查询不变。
- 修改 `app/schemas/rag.py` 或相关注释，明确 v4 接入 Reranker 后 `SourceCitation.score` 表示当前最终排序分，可能为 Reranker 分数或降级后的 RRF 分数。
- 增加单元测试和 API 测试，覆盖精排成功、超时降级、异常降级、结果映射失败降级、低置信度过滤、权限短路和响应兼容。

### Out of Scope

本阶段不做：

- 不新增 HTTP 路径、前端页面或用户可见的精排开关。
- 不重新实现向量检索、全文检索、RRF 融合、HyDE 或多路查询扩展。
- 不改变 `RagQueryRequest`、`RagQueryResponse` 的字段结构。
- 不把 Reranker 分数解释为最终答案置信度，也不把它作为唯一防幻觉依据。
- 不实现第 16 章精确 token 预算裁剪；本阶段只新增同名占位服务并原样返回候选，仍复用现有 `SourceBuilder` 近似字符预算。
- 不实现第 17 章完整引用溯源和防幻觉二次校验。
- 不实现 SSE 流式输出、多轮会话记忆或会话标题生成。
- 不新增数据库表、迁移或索引任务状态。
- 不引入 `dashscope` Python SDK 新依赖；当前阶段使用现有 `httpx`。

## 2.3 预计文件变更清单

### 新增文件

- `app/services/reranker.py`：Reranker 服务，负责精排调用编排、结果映射、候选重排和 RRF 降级。
- `app/services/confidence_filter.py`：低置信度过滤器，按 Reranker 分数过滤候选并处理全低分边界。
- `app/services/context_trimmer.py`：上下文裁剪占位服务，当前只返回输入候选，预留第 16 章 token 预算裁剪替换点。
- `app/services/rag_query_v4.py`：Reranker 精排版 RAG 查询管道，复用 v3 的增强检索入口、固定拒答和生成语义，但不改动 v3 文件。
- `tests/services/test_reranker.py`：覆盖精排成功排序、候选数量控制、超时降级、异常降级、空结果降级和映射失败降级。
- `tests/services/test_confidence_filter.py`：覆盖阈值过滤、全部低分保留最高分、空候选和顺序保持。
- `tests/services/test_context_trimmer.py`：覆盖上下文裁剪占位服务原样返回候选。
- `tests/services/test_rag_query_v4.py`：覆盖 v4 独立管道中的 Reranker 接入、降级、过滤、上下文裁剪占位、拒答和生成行为。

### 修改文件

- `app/integrations/dashscope.py`：改造 `DashScopeRerankerClient` 的入参、返回模型、请求 payload、错误处理和 `usage.total_tokens` 解析。
- `app/api/routes/rag.py`：为 `v4` 查询服务注入 `DashScopeRerankerClient`、`RerankerService`、`ConfidenceFilter` 和 `ContextTrimmer`；`v1/v2/v3` 依赖组装语义保持不变。
- `app/schemas/rag.py`：更新 `SourceCitation.score` 注释，说明 v4 可能返回 Reranker 分数，精排降级时返回 RRF 分数。
- `tests/api/test_rag.py`：补充权限失败时不触发 Reranker 的断言；补充 v4 依赖组装。
- `tests/test_initialization.py`：如配置校验测试需要，补充 `RERANKER_MODEL=qwen3-rerank`、`reranker_top_n`、`reranker_timeout_ms` 和 `rag_min_score` 断言。

### 删除文件

不涉及。

## 2.4 输入/输出模型

### 2.4.1 HTTP 输入

接口保持不变：

```plain
POST /api/v1/rag/query
Content-Type: application/json
```

请求体继续使用 `RagQueryRequest`：

```python
class RagQueryRequest(BaseModel):
    question: str
    kb_ids: list[int]
    session_id: str | None = None
```

校验规则保持不变：

- `question` 去除首尾空白后长度为 1-2000。
- `kb_ids` 至少 1 个，最多 20 个；去重后均为正整数。
- 路由层在查询服务调用前逐个执行读权限校验。
- `session_id` 本阶段仍不读取或写入多轮会话。

### 2.4.2 HTTP 输出

响应结构保持 `RagQueryResponse` 不变：

```python
class RagQueryResponse(BaseModel):
    answer: str
    sources: list[SourceCitation]
    hit_count: int
    latency_ms: int
```

输出语义：

- `answer` 来自真实召回 chunk 组装的上下文，或固定拒答文案。
- `sources` 只包含实际进入 Prompt 的引用 chunk。
- `hit_count` 与 `sources` 数量一致。
- `sources[].score` 在精排成功时为 Reranker `relevance_score`；精排降级时为前序 RRF 分数。
- 不新增 `reranker_status`、`reranker_score_type` 或调试字段；精排状态只进入日志和测试可观察对象。

### 2.4.3 内部 Reranker 模型

新增内部模型建议：

```python
@dataclass(frozen=True)
class RerankApiResult:
    index: int
    relevance_score: float


@dataclass(frozen=True)
class RerankApiResponse:
    results: list[RerankApiResult]
    total_tokens: int | None


@dataclass(frozen=True)
class RerankResult:
    hits: list[ChunkSearchHit]
    degraded: bool
    degraded_reason: str | None
    input_count: int
    output_count: int
    elapsed_ms: int
    total_tokens: int | None
```

模型规则：

- `RerankApiResult.index` 必须对应传入 `documents` 的下标。
- `RerankApiResult.relevance_score` 为当前 Reranker 模型返回分数，通常在 0-1 范围内，但实现不应假设所有模型永远固定该区间。
- `RerankResult.hits` 中的 `ChunkSearchHit.score` 替换为精排分数；降级时保持原 RRF 分数。
- `degraded=True` 表示未使用精排排序，而是回退到前序候选顺序。
- `total_tokens` 来自 provider 返回的 `usage.total_tokens`；缺失时为 `None`。

### 2.4.4 Reranker 客户端输入输出

`DashScopeRerankerClient` 建议方法：

```python
async def rerank(
    self,
    *,
    query: str,
    documents: list[str],
    top_n: int,
) -> RerankApiResponse:
    """调用 DashScope Reranker，返回带原始下标的精排结果。"""
```

HTTP 请求体：

```json
{
  "model": "qwen3-rerank",
  "input": {
    "query": "用户原始问题",
    "documents": ["候选 chunk 1", "候选 chunk 2"]
  },
  "parameters": {
    "top_n": 5,
    "return_documents": false
  }
}
```

鉴权：

```plain
Authorization: Bearer <dashscope_api_key>
Content-Type: application/json
```

HTTP 响应成功字段：

```json
{
  "output": {
    "results": [
      {"index": 0, "relevance_score": 0.91},
      {"index": 2, "relevance_score": 0.77}
    ]
  },
  "usage": {
    "total_tokens": 123
  }
}
```

实现要求：

- 请求使用 `settings.reranker_endpoint`、`settings.reranker_model`、`settings.dashscope_api_key` 和 `settings.reranker_timeout_ms`；其中 `settings.reranker_model` 必须由环境变量 `RERANKER_MODEL=qwen3-rerank` 提供或覆盖为该值。
- `return_documents=false`，避免 provider 返回重复正文造成额外传输。
- `top_n` 由调用方传入，最终不得超过 `len(documents)`。
- `httpx.TimeoutException`、`httpx.HTTPStatusError`、JSON 结构缺失和结果字段类型错误都交给 `RerankerService` 降级处理。

## 2.5 代码执行

### 2.5.1 总体调用链路

```plain
query_rag(...)
  -> PermissionService.require_read(...)
  -> RagQueryServiceV4.query(...)
  -> EnhancedRetriever.retrieve(...)
  -> RerankerService.rerank(question, candidates)
     -> DashScopeRerankerClient.rerank(...)
     -> map provider index back to ChunkSearchHit
     -> fallback to RRF candidates when timeout/error/invalid result
  -> ConfidenceFilter.filter(...)
  -> ContextTrimmer.trim(...)
  -> SourceBuilder.build(...)
  -> chat_model.ainvoke([SystemMessage(...), HumanMessage(...)])
  -> RagQueryResponse
```

执行顺序必须满足：

1. 权限校验在路由层完成。
2. 增强检索只返回已授权候选。
3. Reranker 只处理增强检索返回的候选。
4. Reranker 失败时回退候选，不抛出用户可见错误。
5. 低置信度过滤发生在上下文裁剪占位之前。
6. 上下文裁剪占位发生在 `SourceBuilder` 之前，本阶段只原样返回候选。
7. 无候选或过滤后不可用时返回固定拒答，不调用聊天模型。

### 2.5.2 DashScope Reranker 客户端

修改文件：

```plain
app/integrations/dashscope.py
```

职责：

- 只负责 provider HTTP 调用和响应解析。
- 不做候选降级、不做排序、不处理 `ChunkSearchHit`。
- 出错时抛出异常，由服务层统一降级。

建议执行流程：

```plain
1. 校验 query 非空、documents 非空、top_n > 0。
2. 构造 HTTP payload。
3. 使用 httpx.AsyncClient(timeout=reranker_timeout_ms / 1000) 发送 POST。
4. response.raise_for_status()。
5. 解析 output.results，提取 index 和 relevance_score。
6. 解析 usage.total_tokens；没有则为 None。
7. 返回 RerankApiResponse。
```

边界：

- `documents=[]` 时直接返回空结果，不调用外部服务。
- `top_n` 超过 `len(documents)` 时在调用前截断。
- 不在日志中输出完整 documents。
- 不把 `dashscope_api_key` 写入异常信息或日志。

### 2.5.3 RerankerService

新增文件：

```plain
app/services/reranker.py
```

建议构造参数：

```python
class RerankerService:
    def __init__(
        self,
        *,
        client: DashScopeRerankerClient,
        top_n: int,
    ) -> None: ...
```

建议公开方法：

```python
async def rerank(
    self,
    *,
    question: str,
    candidates: list[ChunkSearchHit],
) -> RerankResult:
    """对候选 chunk 精排；失败时回退到输入候选的前 top_n 条。"""
```

执行流程：

```plain
1. 标准化 question。
2. 如果 candidates 为空，返回空结果，degraded=False。
3. 计算 effective_top_n = min(top_n, len(candidates))。
4. 如果 len(candidates) <= effective_top_n，可跳过外部精排，返回 candidates[:effective_top_n]，degraded=False，degraded_reason="skipped_not_enough_candidates"。
5. 提取 documents = [hit.content for hit in candidates]。
6. 调用 client.rerank(query=question, documents=documents, top_n=effective_top_n)。
7. 校验每个 result.index 在 candidates 范围内，且 relevance_score 可转为 float。
8. 按 relevance_score 降序排序并映射回原 ChunkSearchHit。
9. 返回 score 替换为 relevance_score 的 ChunkSearchHit。
10. 任一异常进入降级：返回 candidates[:effective_top_n]，degraded=True，并记录 degraded_reason。
```

降级原因建议枚举字符串：

- `reranker_timeout`
- `reranker_http_error`
- `reranker_empty_result`
- `reranker_invalid_index`
- `reranker_invalid_score`
- `reranker_unexpected_error`

跳过精排的 `skipped_not_enough_candidates` 不视为失败降级，但应进入 INFO 日志，便于线上确认“进入了 RerankerService，但因候选数量不足未调用外部精排”。

映射规则：

```plain
provider result.index = 传入 candidates/documents 的下标
reranked hit = candidates[result.index]
reranked hit.score = result.relevance_score
```

若 provider 返回重复 index：

- 只保留第一次出现的 index。
- 如果去重后为空，视为 `reranker_empty_result` 并降级。

若 provider 返回少于 `effective_top_n` 条：

- 允许返回较少条候选，但必须至少 1 条。
- 不用 RRF 候选补齐，因为 provider 的 `top_n` 语义允许只返回最相关结果。

### 2.5.4 ConfidenceFilter

新增文件：

```plain
app/services/confidence_filter.py
```

建议构造参数：

```python
class ConfidenceFilter:
    def __init__(self, *, min_score: float) -> None: ...
```

建议公开方法：

```python
def filter(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
    """过滤低置信度候选；全部低分时保留最高分 1 条。"""
```

执行规则：

1. `hits=[]` 时返回 `[]`。
2. 保留 `hit.score >= min_score` 的候选，顺序不变。
3. 如果过滤后非空，返回过滤结果。
4. 如果过滤后为空，返回原列表中 `score` 最高的一条。
5. 当 `min_score <= 0` 时等价于不过滤。

本阶段采用“全部低分时保留最高分 1 条”的策略，原因是后续生成 Prompt 和固定拒答规则仍会约束回答必须基于上下文；完全清空可能让边界问题过早拒答，且第 17 章会继续补充防幻觉二次校验。

注意：

- 该过滤器在精排成功后使用 Reranker 分数。
- 如果 Reranker 降级，它会使用 RRF 分数做过滤。由于 RRF 分数尺度通常低于 0.5，降级时不应直接套用 `rag_min_score=0.5`，否则会频繁只保留 1 条。
- 为避免分数尺度混淆，`RagQueryServiceV4` 只在真实执行 Reranker 且 `RerankResult.degraded=False`、`degraded_reason` 不是 `skipped_not_enough_candidates` 时执行 `ConfidenceFilter`；降级或候选数不足跳过精排时都跳过置信度过滤，直接使用 RRF 候选。

### 2.5.5 ContextTrimmer 占位服务

新增文件：

```plain
app/services/context_trimmer.py
```

`ContextTrimmer` 是第 15 章为了打通完整查询管道而新增的占位模块。它只定义上下文裁剪的稳定入口，本阶段不做 token 预算裁剪，第 16 章会用正式实现整体替换或扩展该文件。

建议构造参数：

```python
class ContextTrimmer:
    def trim(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        """上下文裁剪占位：本阶段原样返回候选。"""
```

执行规则：

1. `hits=[]` 时返回 `[]`。
2. 非空候选按原顺序原样返回。
3. 不读取 `rag_context_max_tokens`，不计算 token，不截断 chunk 内容。
4. 不改变 `ChunkSearchHit.score`、文档来源、页码、章节和 chunk 元数据。
5. 不吞异常；当前实现无外部依赖，正常情况下不应抛出业务异常。

本阶段保留 `SourceBuilder` 的近似字符预算，避免 prompt 过长。`ContextTrimmer` 只负责提供第 16 章正式 token 裁剪的替换点，避免后续再改 v4 主流程形状。

### 2.5.6 RagQueryServiceV4 独立查询管道

新增文件：

```plain
app/services/rag_query_v4.py
```

`RagQueryServiceV4` 是第 15 章新增查询管道。它可以复用 `RagQueryServiceV3` 的固定拒答、生成 Prompt 和异常语义，但必须以新文件承载精排流程，禁止在 `rag_query_v3.py` 上直接叠加 Reranker 逻辑。

建议构造参数：

```python
class RagQueryServiceV4:
    def __init__(
        self,
        *,
        retriever: EnhancedRetriever,
        reranker: RerankerService,
        confidence_filter: ConfidenceFilter,
        context_trimmer: ContextTrimmer,
        source_builder: SourceBuilder,
        chat_model: Any,
        settings: Settings,
    ) -> None: ...
```

主流程调整：

```plain
1. retrieve_result = await self.retriever.retrieve(...)
2. if not retrieve_result.hits: return refusal
3. rerank_result = await self.reranker.rerank(question=normalized_question, candidates=retrieve_result.hits)
4. hits = rerank_result.hits
5. if not rerank_result.degraded:
       hits = self.confidence_filter.filter(hits)
6. if not hits: return refusal
7. hits = self.context_trimmer.trim(hits)
8. if not hits: return refusal
9. context, sources = self.source_builder.build(hits, return_top_n=settings.rag_return_top_n)
10. if not sources or not context: return refusal
11. answer = await self._generate_answer(...)
12. return RagQueryResponse(...)
```

日志要求：

- `INFO` 记录查询开始和完成，不输出正文。
- `INFO` 记录 Reranker 成功、候选数不足跳过精排等关键状态：`[Reranker] 精排完成：候选=...，返回=...`、`[Reranker] 候选数不超过 topN，跳过精排：候选=...，topN=...`。
- `DEBUG` 记录 `original_count`、`hyde_count`、`merged_count`、`reranker_input_count`、`reranker_output_count`、`reranker_elapsed_ms`、`reranker_degraded`、`confidence_filtered_count`。
- `WARNING` 只记录 provider 异常摘要，不输出 API key、完整 chunk 或 Prompt。

异常语义：

- 原始增强检索中的 Embedding 或数据库异常仍按当前 v3 语义返回 503 或 500。
- Reranker 异常不向用户返回 5xx，而是降级到 RRF。
- 聊天模型异常仍返回 503。
- `RagQueryServiceV3` 保持原有 HyDE 增强查询行为，不接收 `RerankerService`、`ConfidenceFilter` 或 `ContextTrimmer` 依赖。

### 2.5.7 依赖组装

修改文件：

```plain
app/api/routes/rag.py
```

新增 `rag_query_pipeline="v4"` 组装顺序：

```plain
EmbeddingService
ChunkRepository
HybridRetriever
QueryRewriter
EnhancedRetriever
DashScopeRerankerClient
RerankerService
ConfidenceFilter
ContextTrimmer
RagQueryServiceV4
```

依赖参数来源：

- `RerankerService.top_n = settings.reranker_top_n`
- `ConfidenceFilter.min_score = settings.rag_min_score`
- `ContextTrimmer` 本阶段无配置参数；第 16 章正式实现时再接入 `rag_context_max_tokens` 或 token 预算配置。
- `DashScopeRerankerClient` 继续从 `get_settings()` 或构造参数读取 `reranker_endpoint`、`reranker_model`、`reranker_timeout_ms` 和 `dashscope_api_key`
- `settings.reranker_model` 在本项目中按部署约定设置为 `qwen3-rerank`，对应环境变量为 `RERANKER_MODEL=qwen3-rerank`

依赖组装要求：

- `rag_query_pipeline="v1"` 继续返回基础向量查询管道。
- `rag_query_pipeline="v2"` 继续返回混合检索查询管道。
- `rag_query_pipeline="v3"` 继续返回 HyDE 增强查询管道，不注入 Reranker。
- `rag_query_pipeline="v4"` 返回 Reranker 精排查询管道。
- 非 `v1/v2/v3/v4` 配置值返回明确配置错误。

推荐实现时优先让 `DashScopeRerankerClient` 支持显式传入 `settings`，减少全局 `get_settings()` 对测试的影响；若保留当前无参构造，测试需通过 settings patch 覆盖。

### 2.5.8 事务边界和状态更新

本阶段不新增数据库写入，不开启新事务，不修改文档、chunk、会话或索引任务状态。

外部状态：

- Reranker 调用不写 Redis。
- Reranker 调用不写数据库。
- `usage.total_tokens` 本阶段只进入日志；后续第 21 章成本监控再统一落指标或持久化。

## 2.6 技术约束与最佳实践

- Reranker 必须位于权限校验和增强检索之后，只处理已授权候选，避免外部服务接触无权限 chunk。
- Reranker 超时和 provider 异常必须降级到 RRF 候选，不能让精排成为查询主链路单点故障。
- 精排成功和降级时的 `score` 尺度不同；低置信度过滤只作用于精排成功结果，避免用 RRF 分数误触发过滤。
- Reranker 客户端必须返回 provider `index`，服务层必须按下标映射回原候选，不能只按返回顺序或分数列表猜测对应关系。
- 本阶段固定使用阿里云 `qwen3-rerank`；除非另开同步任务，不在实现中自动回退到 `gte-rerank-v2` 或其它 rerank 模型。
- 本阶段复用 `httpx` 和现有配置，不新增 DashScope SDK 依赖；如果实现阶段改用 SDK，必须同步更新文档和依赖文件。

## 2.7 验收标准

### 场景一：权限失败时不触发精排

GIVEN 用户请求 `kb_ids=[2, 3]`，其中 `3` 无读权限  
WHEN 调用现有 RAG 查询接口  
THEN 返回 403，且不调用 `EnhancedRetriever`、`RerankerService`、`DashScopeRerankerClient`、`SourceBuilder` 或聊天模型。

### 场景二：精排成功后按相关性重排

GIVEN 增强检索返回 `chunk_id=[10, 11, 12]`，Reranker 返回 `index=2 score=0.91`、`index=0 score=0.82`  
WHEN 执行 v4 查询  
THEN 进入 `SourceBuilder` 的候选顺序为 `[12, 10]`，且对应 `SourceCitation.score` 分别为 `0.91` 和 `0.82`。

### 场景三：精排请求只使用原始问题

GIVEN 查询改写生成了 HyDE 文本  
WHEN 调用 Reranker  
THEN `query` 参数等于用户原始问题，不使用 HyDE 文本替代问题。

### 场景四：精排超时降级到 RRF

GIVEN `DashScopeRerankerClient.rerank(...)` 抛出超时异常  
WHEN 执行 v4 查询  
THEN 服务继续使用增强检索返回的前 `reranker_top_n` 条 RRF 候选，不返回 5xx。

### 场景五：精排 HTTP 错误降级到 RRF

GIVEN provider 返回 429 或 500，客户端抛出 HTTP 异常  
WHEN 执行 RerankerService  
THEN 返回 `degraded=True`，`degraded_reason="reranker_http_error"`，候选顺序保持 RRF 原顺序。

### 场景六：精排返回空结果降级

GIVEN provider 返回成功响应但 `output.results=[]`  
WHEN 执行 RerankerService  
THEN 返回 RRF 前 `top_n` 候选，`degraded=True`，`degraded_reason="reranker_empty_result"`。

### 场景七：精排 index 越界降级

GIVEN provider 返回 `index=99`，但输入候选只有 3 条  
WHEN 执行 RerankerService  
THEN 返回 RRF 前 `top_n` 候选，`degraded=True`，`degraded_reason="reranker_invalid_index"`。

### 场景八：候选数量不足时跳过外部精排

GIVEN 增强检索返回 3 条候选，`reranker_top_n=5`  
WHEN 执行 RerankerService  
THEN 不调用 `DashScopeRerankerClient`，直接返回 3 条候选，且不标记为失败降级。

### 场景九：低置信度过滤保留高分候选

GIVEN 精排成功返回分数 `[0.92, 0.41, 0.12]`，`rag_min_score=0.5`  
WHEN 执行 `ConfidenceFilter.filter(...)`  
THEN 只保留分数 `0.92` 的候选，顺序不变。

### 场景十：全部低分时保留最高分一条

GIVEN 精排成功返回分数 `[0.21, 0.19, 0.08]`，`rag_min_score=0.5`  
WHEN 执行 `ConfidenceFilter.filter(...)`  
THEN 返回分数 `0.21` 的候选，避免上下文直接为空。

### 场景十一：降级时不执行低置信度过滤

GIVEN RerankerService 因超时降级，RRF 候选分数均低于 `rag_min_score`  
WHEN 执行 v4 查询  
THEN 查询服务跳过 `ConfidenceFilter`，继续使用 RRF 前 `top_n` 候选进入 `SourceBuilder`。

### 场景十二：无候选时固定拒答

GIVEN 增强检索没有返回候选  
WHEN 执行 v4 查询  
THEN 返回固定拒答文案，`sources=[]`，`hit_count=0`，且不调用 Reranker 或聊天模型。

### 场景十三：SourceBuilder 空上下文时固定拒答

GIVEN 精排和过滤后仍有候选，但字符预算导致 `SourceBuilder.build(...)` 返回空上下文或空 sources  
WHEN 执行 v4 查询  
THEN 返回固定拒答文案，不调用聊天模型。

### 场景十四：上下文裁剪占位原样返回候选

GIVEN v4 查询中精排和置信度过滤后得到候选 `[chunk_id=12, chunk_id=10]`  
WHEN 执行 `ContextTrimmer.trim(...)`  
THEN 返回候选顺序、数量、内容、score 和来源元数据均不变。

### 场景十五：v4 查询调用上下文裁剪占位

GIVEN v4 查询中精排和置信度过滤后仍有候选  
WHEN 执行查询主流程  
THEN 查询服务先调用 `ContextTrimmer.trim(...)`，再调用 `SourceBuilder.build(...)`。

### 场景十六：Reranker 客户端解析 usage

GIVEN provider 响应包含 `usage.total_tokens=123`  
WHEN `DashScopeRerankerClient.rerank(...)` 返回  
THEN `RerankApiResponse.total_tokens == 123`，供日志和后续成本统计使用。

### 场景十七：响应字段保持兼容

GIVEN v4 查询精排成功并返回 2 个 sources  
WHEN 前端收到响应  
THEN 响应仍只包含 `answer`、`sources`、`hit_count`、`latency_ms`，不包含精排调试字段。

### 场景十八：v3 查询管道保持不变

GIVEN `rag_query_pipeline="v3"`  
WHEN 调用查询接口  
THEN 仍使用 HyDE 增强查询管道，不构造 `RerankerService`、`ConfidenceFilter`、`ContextTrimmer` 或 `RagQueryServiceV4`。

### 场景十九：日志包含精排统计但不泄露正文

GIVEN Reranker 被调用并成功返回  
WHEN 查询完成  
THEN 日志包含输入候选数、输出候选数、耗时、是否降级和 token 使用量，不输出完整 Prompt、API Key 或大段 chunk 正文。

### 场景二十：文档同步

GIVEN 实现阶段调整了 Reranker provider、返回字段、阈值策略、上下文裁剪占位或依赖文件  
WHEN 生成 Process 文档  
THEN 必须记录实际差异；如果行为偏离本 Spec，先同步更新本 Spec。
