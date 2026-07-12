# 16-上下文裁剪——Token 预算控制 Spec

本文基于 [16-上下文裁剪——Token 预算控制-plan.md](../plans/16-上下文裁剪——Token%20预算控制-plan.md)、参考资料 [16-上下文裁剪——Token 预算控制.md](../references/16-上下文裁剪——Token%20预算控制.md)，以及 [三阶段生成.md](../prompt/三阶段生成.md) 的 Spec 阶段规则，定义 Python / FastAPI / LangChain 版本的上下文裁剪与 Token 预算控制技术方案。

本阶段只替换第 15 章留下的 `ContextTrimmer` 占位实现：在 `RagQueryServiceV4` 中，继续沿用“增强检索 -> Reranker 精排 -> 低置信度过滤 -> 上下文裁剪 -> SourceBuilder -> 生成回答”的主流程，但把上下文裁剪从“原样返回候选”升级为“按 token 预算保留高相关 chunk”。接口路径、请求体、响应体、权限入口、检索、精排、置信度过滤和生成 Prompt 不新增用户可见能力。

## 前置自检

眼下没有完全把握的事情有三类：

- DashScope / DeepSeek 兼容模型的真实 tokenizer 与 `tiktoken` 的 `cl100k_base` 是否完全一致；如果不一致，计数会有小幅误差。
- 当前 `SourceBuilder` 仍保留字符预算安全网；第 16 章正式裁剪后，字符预算可能在极端英文长文本下再次截断内容。
- 项目当前通过 `prometheus-fastapi-instrumentator` 暴露 HTTP `/metrics`，但尚未配置独立监控系统；本阶段使用 OpenTelemetry Counter 保留标准指标模型，同时按参考资料在每次记录时立即输出控制台日志，不接入 OTLP Exporter 或 Collector。

影响与处理：

- 已按 `$find-docs` 要求通过 Context7 查询 `tiktoken` 文档：Python 可用 `tiktoken.get_encoding("cl100k_base")` 获取编码器，用 `encode(...)` 计数，用 `decode(tokens[:max_tokens])` 截断后还原文本。本阶段采用 `cl100k_base` 作为稳定工程近似，并在配置和文档中明确这不是 provider 账单 token 的绝对值。
- 已通过 Context7 核对 OpenTelemetry Python 官方文档：`MeterProvider` 提供 Metrics SDK，`meter.create_counter(...)` 创建计数器，`counter.add(...)` 记录增量。本阶段不配置 `MetricReader` 或 Exporter，控制台可见数据由 `TokenMetrics` 的即时 `INFO` 日志提供。
- 后续接入监控系统时再为同一 `MeterProvider` 配置 OTLP 或其他 `MetricReader`；`TokenMetrics` 的 Counter 名称和业务调用方式保持不变。
- 本阶段把 `rag_context_max_tokens` 定义为“参考 chunk 正文预算”，不包含 System Prompt、用户问题、回答预留和 SourceBuilder 的元数据前缀；整体 Prompt 预算和多轮历史压缩留到后续流式与多轮阶段。

## 2.1 任务背景

第 15 章已经完成 Reranker 精排和降级路径，并在 v4 查询管道中预留了 `ContextTrimmer` 占位服务。当前占位实现只是原样返回候选，真正进入 Prompt 的内容仍主要依赖 `SourceBuilder` 的近似字符预算，无法按模型 token 成本做精确控制。

本阶段目标是在生成回答前引入正式上下文裁剪：

```plain
用户问题
  -> 权限校验
  -> 查询改写 + 混合检索 + 二次 RRF
  -> Reranker 精排或降级
  -> 低置信度过滤
  -> 按 token 预算裁剪候选 chunk
  -> SourceBuilder 构建上下文和引用
  -> Chat 模型基于裁剪后的真实上下文生成回答
```

技术前提：

- `app/services/context_trimmer.py` 已存在占位服务，当前 `trim(...)` 只返回输入候选。
- `app/services/rag_query_v4.py` 已在 `SourceBuilder.build(...)` 前调用 `context_trimmer.trim(...)`。
- `app/core/config.py` 已有 `rag_context_max_tokens: int = 3000`。
- `app/repositories/chunks.py` 中的 `ChunkSearchHit` 是冻结 dataclass，裁剪单个 chunk 内容时需要创建新对象而不是原地修改。
- `pyproject.toml` 已显式依赖 `tiktoken>=0.7.0`。

核心目标：

- 按已排序候选顺序贪心选择 chunk，在 `rag_context_max_tokens` 内尽量保留高相关内容。
- 首个 chunk 超预算时截断其内容并保留来源元数据，避免有高相关证据却完全没有上下文。
- 后续 chunk 超预算时停止追加，避免低优先级内容挤占成本。
- 记录每次查询的候选数量、选中数量、使用 token、预算上限和是否发生截断。
- 无可用上下文时沿用固定拒答，不调用聊天模型自由发挥。

## 2.2 范围对齐

### In Scope

- 将 `ContextTrimmer` 从占位服务升级为正式 token 预算裁剪服务。
- 使用 `tiktoken` 的 `cl100k_base` 编码统计 chunk 正文 token 数。
- 支持按已排序候选贪心选择，预算耗尽后停止追加。
- 支持首个 chunk 超预算时按 token 截断正文，并用新 `ChunkSearchHit` 保留原来源元数据。
- 保持 `ContextTrimmer.trim(...)` 输入输出类型一致：输入和输出均为 `list[ChunkSearchHit]`，`used_tokens` 只作为方法内部局部变量。
- 修改 `RagQueryServiceV4`，消费裁剪后的候选列表并在无候选时固定拒答。
- 新增 token 指标服务，使用 OpenTelemetry Metrics 对齐参考资料记录三类消耗：Embedding 消耗的 Token 总数、裁剪后 RAG chunk 正文的 Context Token 总数、模型生成消耗的 Token 总数；OpenTelemetry / Redis 记录失败不影响问答。
- 新增 OpenTelemetry Metrics 初始化和关闭流程；Token 记录每次立即写入控制台日志，不做周期导出。
- Embedding 改用 OpenAI-compatible SDK 原始响应，在 provider 返回 `usage.total_tokens` 时记录真实 Token；缺失 usage 时记录 `embedding_token_usage_unavailable=true`。Generation Token 在 v1-v4 生成响应暴露 usage 时记录，Context Token 由 v4 `ContextTrimmer` 在裁剪完成后记录。
- 保持 HTTP 请求、响应字段和权限校验顺序不变。
- 增加单元测试和查询管道测试，覆盖预算内、预算耗尽、首个 chunk 截断、空预算、引用元数据不丢失、指标失败降级。

### Out of Scope

- 不重新实现向量检索、全文检索、RRF 融合、查询改写、Reranker 精排或低置信度过滤。
- 不新增 HTTP 路径、前端页面、用户可见预算开关或成本看板。
- 不改变 `RagQueryRequest`、`RagQueryResponse` 或 `SourceCitation` 的字段结构。
- 不实现整体 Prompt 预算分配、对话历史压缩、回答 token 预估或 SSE 流式输出。
- 不实现完整引用溯源和防幻觉二次校验。
- 不把 token 预算结果作为答案置信度，也不把它暴露为用户响应字段。
- 不新增数据库表或迁移；本阶段的 Token 成本记录只进入日志、OpenTelemetry Counter 和 Redis Hash。
- 不在本阶段迁移 `prometheus-fastapi-instrumentator` 已有 HTTP 指标，不引入 traces/logs，不新建 Grafana 仪表盘。
- 不在本阶段配置 OTLP Exporter、Collector、监控后端或相关容器编排，统一留到后续监控阶段处理。
- 不依赖 provider 返回的真实账单 token 来做裁剪；裁剪以本地 tokenizer 结果为准。

## 2.3 预计文件变更清单

### 新增文件

- `app/core/telemetry.py`：初始化不带 Exporter 的 OpenTelemetry `MeterProvider`，并在应用关闭时 shutdown。
- `app/services/token_metrics.py`：RAG token 消耗指标服务，封装 Embedding、Context、Generation 三类 OpenTelemetry Counter 和 Redis Hash 写入，失败时只记录日志。

### 修改文件

- `app/services/context_trimmer.py`：替换占位实现，注入 `TokenMetrics`，新增 token 计数、贪心选择、首个 chunk 截断和方法末尾 Context Token 埋点。
- `app/services/rag_query_v4.py`：异步调用 `ContextTrimmer.trim(...)` 并直接消费返回的候选列表；若聊天模型响应暴露 usage，则记录 Generation Token。
- `app/services/rag_query.py`、`app/services/rag_query_v2.py`、`app/services/rag_query_v3.py`：注入同一 `TokenMetrics`，在聊天模型响应暴露 usage 时记录 Generation Token；不改变既有检索、拒答和响应语义。
- `app/integrations/openai_embeddings.py`：直接调用 OpenAI-compatible `embeddings.create()`，按响应下标恢复向量顺序并返回 `usage.total_tokens`。
- `app/services/embedding.py`：保持 `embed_documents()` / `embed_query()` 只返回向量；内部在 provider 暴露真实 usage 时调用 token 指标服务，缺失时继续记录 unavailable，不伪造精确值。
- `app/api/routes/rag.py`：新增 `TokenMetrics` 依赖获取，为 v1-v4 查询服务注入同一应用级实例；v4 额外组装正式 `ContextTrimmer`。
- `app/main.py`：在 FastAPI lifespan 中初始化 OpenTelemetry Metrics 和应用级 `TokenMetrics`，并在关闭时 shutdown；已有 HTTP `/metrics` 暴露逻辑保持不变。
- `pyproject.toml`：新增直接依赖 `opentelemetry-api` 和 `opentelemetry-sdk`，不引入 OTLP Exporter 或新的 Prometheus 依赖。
- `tests/services/test_context_trimmer.py`：替换占位测试，覆盖 token 预算裁剪、首个 chunk 截断、空候选、空预算、元数据保留和方法末尾 Context Token 记录。
- `tests/core/test_telemetry.py`：使用 InMemoryMetricReader 覆盖 MeterProvider 初始化、关闭和未启用监控时的边界。
- `tests/services/test_token_metrics.py`：使用 InMemoryMetricReader 覆盖 Embedding、Context、Generation 三类 OpenTelemetry / Redis 记录、即时 `INFO` 日志、从 `current_user_var` 获取可选用户，以及 Redis 异常不影响主流程。
- `tests/services/test_rag_query_v4.py`：更新异步 fake trimmer，使其直接返回候选列表，覆盖裁剪后无候选拒答。
- `tests/services/test_rag_query.py`、`tests/services/test_rag_query_v2.py`、`tests/services/test_rag_query_v3.py`：补充 Generation Token usage 可用与不可用时的行为。
- `tests/api/test_rag.py`：补充应用级 `TokenMetrics` 依赖获取、v1-v4 注入和 v4 `ContextTrimmer` 组装断言，保持权限失败短路。

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

- `answer` 来自裁剪后真实 chunk 组装的上下文，或固定拒答文案。
- `sources` 只包含裁剪后且实际进入 Prompt 的引用 chunk。
- `hit_count` 与 `sources` 数量一致。
- 不新增 `used_tokens`、`max_tokens`、`truncated` 或调试字段；`used_tokens` 只进入日志和 Context Token 指标，其余裁剪状态只进入日志。

### 2.4.3 ContextTrimmer 输入输出

`ContextTrimmer` 构造参数：

```python
class ContextTrimmer:
    def __init__(
        self,
        *,
        max_context_tokens: int,
        token_metrics: TokenMetrics,
        encoding_name: str = "cl100k_base",
    ) -> None: ...
```

公开方法：

```python
async def trim(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
    """按 token 预算裁剪候选 chunk，保留原始排序和来源元数据。"""
```

输入规则：

- `hits` 必须是前序已经排序的候选，通常来自 Reranker 成功结果或 RRF 降级结果。
- `max_context_tokens <= 0` 时返回空列表，不抛异常。
- 空候选返回空列表。

输出规则：

- 返回值是裁剪后的 `list[ChunkSearchHit]`，与输入类型一致，候选相对顺序不变。
- 首个 chunk 超预算时，返回列表中的该候选是保留原来源元数据、仅替换 `content` 的新对象。
- `used_tokens`、输入候选数、选中候选数和截断状态不通过返回值传播；其中 `used_tokens` 用于预算判断、日志和 Context Token 指标，其余裁剪状态只用于日志。
- `used_tokens` 只统计最终保留的 `ChunkSearchHit.content`，不包含 System Prompt、用户问题、对话历史、SourceBuilder 元数据前缀或生成答案。

### 2.4.4 TokenMetrics 输入输出

新增指标服务：

```python
class TokenMetrics:
    def __init__(
        self,
        redis_client: redis.Redis,
        meter: Meter | None = None,
    ) -> None: ...

    async def record_embedding_tokens(
        self,
        *,
        tokens: int,
        source: str = "provider",
    ) -> None:
        """记录 Embedding 消耗的 Token 总数；失败不影响主流程。"""

    async def record_context_tokens(
        self,
        *,
        tokens: int,
        pipeline: str = "v4",
    ) -> None:
        """记录裁剪后 RAG chunk 正文的 Context Token 总数；失败不影响查询主流程。"""

    async def record_generation_tokens(
        self,
        *,
        tokens: int,
        source: str = "provider",
    ) -> None:
        """记录模型生成消耗的 Token 总数；失败不影响查询主流程。"""
```

OpenTelemetry 指标：

```plain
rag.tokens.embedding{source="provider|estimated"}
rag.tokens.context{pipeline="v4", source="local_tiktoken"}
rag.tokens.generation{source="provider|estimated"}
```

OpenTelemetry Counter 名称与参考资料的 Micrometer 指标名保持一致。本阶段不导出到监控系统；控制台通过 `TokenMetrics` 的即时日志观察单次记录值，Counter 保留为后续接入 Exporter 时的标准指标入口。

Redis Key：

```plain
rag:token-stats:{user_id}
```

Redis Hash Fields：

```plain
embeddingTokens
contextTokens
generationTokens
```

记录规则：

- `embeddingTokens` 累加 Embedding provider 原始响应返回的 `usage.total_tokens`；如果 provider 不返回 usage，不写入该字段，日志记录 `embedding_token_usage_unavailable=true`。
- `contextTokens` 累加 `ContextTrimmer` 传入的 `used_tokens`。
- `generationTokens` 累加聊天模型返回的真实 completion token；如果当前 provider / LangChain 不暴露 usage，不写入该字段，日志记录 `generation_token_usage_unavailable=true`。
- `TokenMetrics` 使用 `current_user_var.get()` 获取可选用户上下文；OpenTelemetry Counter 始终记录全局消耗，Redis Hash 只在当前用户可用时写入 `rag:token-stats:{user_id}`，后台任务等无用户上下文场景只记录 OpenTelemetry。
- `record_context_tokens(...)` 只接收 `tokens` 和低基数 `pipeline`，不记录候选数量、选中数量、预算上限或截断状态。
- `source="estimated"` 仅允许在调用方明确传入估算 token 时使用；本阶段 Context Token 来自 `ContextTrimmer` 的本地 `tiktoken` 计数，只表示裁剪后 RAG chunk 正文的 token 总数。
- Redis 写入异常只记录 warning，不中断查询。

## 2.5 代码执行

### 2.5.1 总体调用链路

```plain
query_rag(...)
  -> PermissionService.require_read(...)
  -> RagQueryServiceV4.query(...)
  -> EnhancedRetriever.retrieve(...)
  -> RerankerService.rerank(...)
  -> ConfidenceFilter.filter(...)                         # 仅真实精排成功时
  -> ContextTrimmer.trim(...)
       -> TokenMetrics.record_context_tokens(used_tokens)
  -> SourceBuilder.build(trimmed_hits, ...)
  -> chat_model.ainvoke([SystemMessage(...), HumanMessage(...)])
  -> TokenMetrics.record_generation_tokens(...)            # 仅模型返回 usage 时
  -> RagQueryResponse
```

执行顺序必须满足：

1. 权限校验在向量化、检索、精排和裁剪之前完成。
2. `ContextTrimmer` 只处理已授权、已排序候选。
3. token 裁剪发生在 `SourceBuilder.build(...)` 之前。
4. `ContextTrimmer.trim(...)` 在方法末尾记录本次裁剪保留的 RAG chunk 正文 token，指标失败不影响候选列表返回和后续生成。
5. 裁剪后无候选时返回固定拒答，不调用 `SourceBuilder` 或聊天模型。
6. 生成完成后，如果聊天模型响应暴露 token usage，则记录 Generation Token；不暴露时只记录 unavailable 日志。
7. `SourceBuilder` 继续负责参考编号、元数据前缀和 `SourceCitation` 构建。

### 2.5.2 ContextTrimmer 裁剪规则

修改文件：

```plain
app/services/context_trimmer.py
```

核心流程：

```plain
1. 初始化 tiktoken 编码器：tiktoken.get_encoding("cl100k_base")。
2. 初始化 selected=[]、used_tokens=0 和 truncated=False。
3. max_context_tokens <= 0 或 hits 为空时跳过候选遍历，继续执行方法末尾的日志和指标记录。
4. 从输入候选第 1 条开始遍历。
5. 统计当前 hit.content 的 token 数。
6. 如果 used_tokens + chunk_tokens <= max_context_tokens，则整条保留并累加 used_tokens。
7. 如果当前是第一条且 selected 为空，则按剩余 token 截断内容，重新统计截断正文 token 后累加 used_tokens，并标记 truncated=True。
8. 如果当前不是第一条且放不进预算，则停止遍历。
9. 记录候选数、选中数、used_tokens、预算上限和截断状态日志。
10. 调用 await token_metrics.record_context_tokens(tokens=used_tokens)，失败由 TokenMetrics 内部降级。
11. 返回 selected，类型为 list[ChunkSearchHit]。
```

计数方法：

```python
def count_tokens(self, text: str | None) -> int:
    if not text or not text.strip():
        return 0
    return len(self._encoding.encode(text))
```

截断方法：

```python
def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
    tokens = self._encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return self._encoding.decode(tokens[:max_tokens]).strip()
```

实现注意：

- 使用 `dataclasses.replace(original, content=truncated)` 创建截断后的 `ChunkSearchHit`，保留 `chunk_id`、`doc_id`、`document_name`、`kb_id`、`chunk_index`、`page_num`、`section_title` 和 `score`。
- 截断后如果文本为空白，则不保留该 chunk，返回空结果。
- 对非首个超预算 chunk 不做部分截断，直接停止追加，避免后排内容打碎引用语义。
- `used_tokens` 必须重新统计截断后的文本，而不是直接使用 `max_tokens`。
- `used_tokens` 是方法内局部变量，不写入 `ContextTrimmer` 实例属性，避免并发请求相互覆盖。
- `trim(...)` 保持异步，是因为方法末尾需要等待 `TokenMetrics` 完成 Redis 写入；不得使用未受控的 `asyncio.create_task()` 绕过等待。
- 不在日志中输出完整 chunk 内容。

### 2.5.3 OpenTelemetry Metrics 与 TokenMetrics

新增文件：

```plain
app/core/telemetry.py
app/services/token_metrics.py
```

OpenTelemetry 初始化：

```python
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource

provider = MeterProvider(
    resource=Resource.create({"service.name": "rag-kb"}),
)
metrics.set_meter_provider(provider)
```

初始化规则：

- `enable_metrics=false` 时不初始化 SDK `MeterProvider`，OpenTelemetry API 保持 no-op；`TokenMetrics` 的即时日志和 Redis 记录仍正常执行。
- `enable_metrics=true` 时在 FastAPI lifespan 启动阶段创建且只创建一次不带 Exporter 的 `MeterProvider`，应用关闭时调用 `shutdown()`。
- `TokenMetrics` 在 Redis 客户端初始化后创建一次并存入 `app.state`；即使 `enable_metrics=false`，仍保留 Redis Hash 记录，OpenTelemetry Counter 由 no-op Meter 承接。
- 本阶段不读取 `OTEL_EXPORTER_OTLP_ENDPOINT`、`OTEL_METRIC_EXPORT_INTERVAL` 等导出配置，也不启动周期导出线程。
- 服务名使用 `settings.app_name`，环境使用 `settings.app_env`；后续新增 Exporter 时继续复用该 `Resource`。

Token Counter 定义：

```python
meter = metrics.get_meter("rag-kb.token-metrics")

embedding_tokens = meter.create_counter(
    "rag.tokens.embedding",
    description="Embedding 消耗的 Token 总数",
)
context_tokens = meter.create_counter(
    "rag.tokens.context",
    description="裁剪后 RAG chunk 正文的 Context Token 总数",
)
generation_tokens = meter.create_counter(
    "rag.tokens.generation",
    description="模型生成消耗的 Token 总数",
)
```

控制台日志与参考资料保持一致：每次记录立即输出，不等待固定时间间隔。

```python
logger.info("[TokenMetrics] record_embedding_tokens=%s", tokens)
logger.info("[TokenMetrics] record_context_tokens=%s", tokens)
logger.info("[TokenMetrics] record_generation_tokens=%s", tokens)
```

日志只输出指标类型和本次 token 增量，不输出 Prompt、chunk 正文、用户问题或 API Key。OpenTelemetry Counter 的累计快照本阶段不输出 JSON；需要查看用户累计值时读取 Redis Hash。

记录流程：

```plain
1. 校验传入各记录方法的 `tokens` 不为负数。
2. 每次有效记录先以 `INFO` 输出本次 token 增量，控制台立即可见。
3. Embedding Token：仅在 provider 或调用方提供可用 token 数时记录。
4. Context Token：`ContextTrimmer.trim(...)` 在方法末尾传入内部累计的 `used_tokens`，指标服务只记录该 token 总数。
5. Generation Token：仅在聊天模型响应暴露 completion token 或调用方提供估算值时记录。
6. OpenTelemetry Counter 通过 `add(tokens, attributes)` 分别累加 embedding/context/generation 三类 token。
7. `TokenMetrics` 从 `current_user_var` 获取当前用户；存在用户时使用 hincrby 累加 Redis 用户维度统计，不存在时跳过 Redis。
8. 任一 Redis 写入失败只记录 warning，不向上抛出；日志或 Counter 记录失败也不能中断主流程。
```

Redis 写入建议：

```plain
HINCRBY rag:token-stats:{user_id} embeddingTokens {tokens}
HINCRBY rag:token-stats:{user_id} contextTokens {used_tokens}
HINCRBY rag:token-stats:{user_id} generationTokens {tokens}
```

边界：

- `used_tokens=0` 时不增加 `contextTokens`，但可在日志中观察空上下文请求。
- Embedding / Generation 没有真实 usage 时不能伪造精确 token；日志记录 unavailable，指标不累加或只在调用方明确标记估算值时以 `source="estimated"` 记录。
- Redis 客户端不可用、连接失败或写入失败时，不影响 `RagQueryResponse`。
- 本阶段不新增读取这些统计的 HTTP 接口。

### 2.5.4 RagQueryServiceV4 调整

修改文件：

```plain
app/services/rag_query_v4.py
```

构造函数新增依赖：

```python
class RagQueryServiceV4:
    def __init__(
        self,
        *,
        retriever: EnhancedRetriever,
        reranker: RerankerService,
        confidence_filter: ConfidenceFilter,
        context_trimmer: ContextTrimmer,
        token_metrics: TokenMetrics,
        source_builder: SourceBuilder,
        chat_model: Any,
        settings: Settings,
    ) -> None: ...
```

`_prepare_context_hits(...)` 保持返回 `list[ChunkSearchHit]`：

```plain
1. 调用 RerankerService.rerank(...)。
2. 真实精排成功时调用 ConfidenceFilter.filter(...)。
3. 按 `rag_return_top_n` 截取允许进入 `SourceBuilder` 的候选，避免裁剪器统计未进入 Prompt 的正文。
4. await ContextTrimmer.trim(...) 得到裁剪后的候选列表；Context Token 已在 trim(...) 方法末尾记录。
5. 返回 list[ChunkSearchHit]。
```

主流程调整：

```plain
trimmed_hits = await self._prepare_context_hits(...)
if not trimmed_hits:
    return refusal
context, sources = self.source_builder.build(trimmed_hits, return_top_n=...)
```

日志要求：

- `ContextTrimmer` 在方法末尾以 `INFO` 记录裁剪摘要：输入候选数、选中候选数、使用 token、预算上限、是否截断。
- `DEBUG` 可记录 Reranker、过滤和裁剪之间的数量变化。
- 不输出完整 Prompt、完整 chunk 正文、API Key 或用户敏感内容。

异常语义：

- `ContextTrimmer` 本地计算不应抛业务异常；如果 tokenizer 初始化配置错误，应在应用启动或依赖组装阶段暴露为配置错误。
- `TokenMetrics` 异常不影响回答生成。
- 裁剪后无候选返回固定拒答，不调用聊天模型。
- 聊天模型异常仍按 v4 现有语义返回 503。

### 2.5.5 依赖组装

修改文件：

```plain
app/api/routes/rag.py
```

TokenMetrics 与 v4 组装变化：

```plain
FastAPI lifespan
  -> init_metrics(settings)
  -> TokenMetrics(redis_client=get_redis())
  -> app.state.token_metrics = token_metrics

get_rag_query_service(..., token_metrics=Depends(get_token_metrics))
  -> EmbeddingService(..., token_metrics=token_metrics)   # 记录 provider usage.total_tokens
  -> RagQueryServiceV1/V2/V3/V4(..., token_metrics=token_metrics)

ContextTrimmer(
    max_context_tokens=settings.rag_context_max_tokens,
    token_metrics=token_metrics,
)
RagQueryServiceV4(..., context_trimmer=context_trimmer, token_metrics=token_metrics, ...)
```

组装要求：

- `TokenMetrics` 是应用级单实例，避免每次请求重复创建同名 OpenTelemetry Counter。
- `get_token_metrics(...)` 只从 `request.app.state` 取已初始化实例，不在路由层创建新 Counter。
- `TokenMetrics` 内部使用 `current_user_var.get()` 获取可选用户上下文，业务调用方不重复传递 `user_id`。
- `rag_query_pipeline="v1"`、`v2`、`v3` 保持现有检索和响应行为，仅新增 Embedding / Generation Token 记录。
- `rag_query_pipeline="v4"` 使用注入同一 `TokenMetrics` 实例的正式 `ContextTrimmer`。
- `SourceBuilder(max_context_chars=settings.rag_context_max_tokens * 4)` 本阶段暂时保留为最终安全网；如果实现后出现二次截断影响验收，优先在 Process 中记录，并单独同步 SourceBuilder 设计。
- `rag_context_max_tokens <= 0` 不阻止应用启动，但查询时裁剪为空并触发固定拒答；测试需固定该行为。

### 2.5.6 事务边界和状态更新

本阶段不新增数据库写入，不开启新事务，不修改文档、chunk、会话或索引任务状态。

外部状态：

- OpenTelemetry Counter 仅在当前进程内累加，本阶段不导出、不持久化；控制台即时日志用于观察单次增量，长期累计由 Redis Hash 保存。
- Redis Hash 是用户维度累计统计，写入失败不回滚查询。
- 本阶段不把 token 统计写入 PostgreSQL。

## 2.6 技术约束与最佳实践

- token 裁剪必须位于 Reranker / RRF 排序之后，且只处理已授权候选，避免低相关内容抢占预算或扩大权限范围。
- `rag_context_max_tokens` 只控制 chunk 正文预算，`tiktoken cl100k_base` 是本地稳定近似，不等同于整次请求窗口或 provider 账单 token。
- `ChunkSearchHit` 是冻结 dataclass，截断正文时必须创建新对象并保留来源元数据，不能原地修改。
- OpenTelemetry `MeterProvider` 和 `TokenMetrics` 必须按应用生命周期单次初始化；本阶段不配置 Exporter，后续接入监控系统时不得修改 `TokenMetrics` 业务接口。
- OpenTelemetry attributes 只允许 `source`、`pipeline` 等低基数字段，不记录 `user_id`、`kb_id` 或文档 ID；导出或 Redis 写入失败不能影响主查询链路。

## 2.7 验收标准

### 场景一：权限失败时不触发裁剪

GIVEN 用户请求 `kb_ids=[2, 3]`，其中 `3` 无读权限
WHEN 调用现有 RAG 查询接口
THEN 返回 403，且不调用 `EnhancedRetriever`、`RerankerService`、`ContextTrimmer`、`TokenMetrics`、`SourceBuilder` 或聊天模型。

### 场景二：按预算贪心保留候选

GIVEN 已排序候选正文 token 数分别为 `[10, 20, 30]`
WHEN 执行 `ContextTrimmer.trim(...)`
THEN 当预算为 100 时返回全部候选并记录 60 token；当预算为 35 时只返回前两条并记录 30 token，候选顺序保持不变。

### 场景三：首个 chunk 超预算时截断并保留来源

GIVEN `rag_context_max_tokens=10`，第一条候选正文超过 10 token，且包含完整来源元数据
WHEN 执行 `ContextTrimmer.trim(...)`
THEN 返回一条正文不超过 10 token 的新 `ChunkSearchHit`，除 `content` 外的来源元数据保持不变，指标记录截断后正文的实际 token 数。

### 场景四：无可用上下文时固定拒答

GIVEN 输入候选为空，或 `rag_context_max_tokens <= 0`
WHEN 执行 v4 查询
THEN `ContextTrimmer` 返回空列表并记录 0 token，查询返回固定拒答，且不调用 `SourceBuilder` 或聊天模型。

### 场景五：Reranker 降级后仍裁剪并使用裁剪后引用

GIVEN Reranker 超时并回退到 RRF 候选，`ContextTrimmer` 最终保留 `[chunk_id=12, chunk_id=10]`
WHEN 执行 v4 查询
THEN 系统跳过低置信度过滤但仍执行 token 裁剪，`SourceBuilder` 和响应引用只使用 `[12, 10]`。

### 场景六：记录三类 Token 消耗

GIVEN Embedding usage 为 120、裁剪后 RAG chunk 正文为 42 token、Generation usage 为 88
WHEN 分别记录 Embedding、Context 和 Generation Token
THEN 控制台立即输出三条对应的 `INFO` 日志，OpenTelemetry Counter 与当前用户 Redis Hash 分别增加 120、42、88；Embedding 或 Generation usage 不可用时不伪造或写入精确值。

### 场景七：Context Token 只统计 RAG chunk 正文

GIVEN 裁剪后 RAG chunk 正文为 42 token，System Prompt、用户问题、对话历史和 SourceBuilder 元数据前缀另占 token
WHEN `ContextTrimmer.trim(...)` 在方法末尾记录 Context Token
THEN OpenTelemetry 和 Redis 只增加 42，不统计其他 Prompt 内容或生成答案。

### 场景八：指标故障不影响查询主流程

GIVEN OpenTelemetry Counter 或 Redis 写入抛出异常
WHEN 查询链路记录 Token 指标
THEN 失败只记录日志，不中断裁剪、引用构建或回答生成。
