# 16-上下文裁剪——Token 预算控制 Process

本文记录 `docs/specs/16-上下文裁剪——Token 预算控制-spec.md` 的实际代码落地。实现范围限定为：将 v4 的上下文裁剪占位替换为正式 token 预算裁剪，增加 RAG Token 日志、OpenTelemetry Counter 与 Redis 用户累计统计，并记录 Embedding provider usage 和 v1-v4 Generation Token。

本阶段按 `$find-docs` 要求通过 Context7 查询 OpenTelemetry Python 文档，采用 `/websites/opentelemetry-python_readthedocs_io_en_stable`。文档确认 `MeterProvider` 可管理 Meter，Counter 使用 `add(amount, attributes)` 累加，`InMemoryMetricReader` 适合单元测试。本阶段按最终 Spec 不配置 MetricReader、Exporter 或 Collector，控制台通过即时 `INFO` 日志观察单次增量，Redis 保存用户累计值。

Embedding usage 补充实现通过 Context7 核对 `/openai/openai-python/v2.11.0`：异步 `embeddings.create()` 返回 `CreateEmbeddingResponse`，可从 `response.usage.total_tokens` 读取 provider 实际 Token。

## 3.1 实际文件变更清单

### 新增文件

- `app/core/telemetry.py`：按 `enable_metrics` 初始化不带 Exporter 的 OpenTelemetry `MeterProvider`，并提供安全关闭入口。
- `app/integrations/openai_embeddings.py`：使用 OpenAI SDK 调用兼容接口，返回按 index 排序的向量和 provider `usage.total_tokens`。
- `app/services/token_metrics.py`：封装 Embedding、Context、Generation 三类 Counter、即时日志、Redis Hash 累加、Generation usage 提取和失败降级。
- `tests/integrations/test_openai_embeddings.py`：覆盖 OpenAI-compatible 响应向量排序和 `usage.total_tokens` 提取。
- `tests/core/test_telemetry.py`：覆盖监控关闭、SDK Provider 初始化、Resource 属性和关闭流程。
- `tests/services/test_token_metrics.py`：使用 `InMemoryMetricReader` 覆盖三类指标、Redis 用户统计、usage 提取、即时日志和 Redis 异常降级。
- `docs/process/16-上下文裁剪——Token 预算控制-process.md`：记录本阶段实际实现、验证结果和 Review 关注点。

### 修改文件

- `app/services/context_trimmer.py`：使用 `tiktoken cl100k_base` 按排序贪心裁剪，首个 chunk 超预算时截断正文并保留元数据，在方法末尾记录实际保留的 RAG 正文 token。
- `app/core/clients.py`：Embedding 客户端改用 OpenAI SDK 异步客户端并交由应用生命周期关闭，服务层继续统一负责重试。
- `app/services/embedding.py`：保持公开方法只返回向量，内部记录真实 Embedding usage；provider 缺失 usage 时记录 `embedding_token_usage_unavailable=true`。
- `app/services/rag_query.py`、`app/services/rag_query_v2.py`、`app/services/rag_query_v3.py`、`app/services/rag_query_v4.py`：注入应用级 `TokenMetrics`，从 `usage_metadata.output_tokens` 或 `response_metadata.token_usage.completion_tokens` 记录 Generation Token。
- `app/services/rag_query_v4.py`：异步调用正式 `ContextTrimmer`，并在裁剪前按 `rag_return_top_n` 限制候选，保证裁剪、引用和 Context Token 使用同一口径。
- `app/api/routes/rag.py`：从 `app.state` 获取单例 `TokenMetrics`，为 v1-v4 注入同一实例，并按配置组装正式 `ContextTrimmer`。
- `app/main.py`：在 FastAPI lifespan 中初始化 MeterProvider 和 TokenMetrics，应用关闭时依次关闭指标提供器和外部客户端。
- `pyproject.toml`、`uv.lock`：通过 `uv add` 增加 `opentelemetry-api==1.43.0` 和 `opentelemetry-sdk==1.43.0` 依赖及锁文件记录。
- `tests/services/test_context_trimmer.py`：替换占位测试，覆盖预算内保留、预算耗尽、首块截断、元数据保留、空候选和非正预算。
- `tests/services/test_embedding.py`：覆盖真实 provider usage 记录和 usage 不可用日志。
- `tests/services/test_rag_query.py`、`tests/services/test_rag_query_v2.py`、`tests/services/test_rag_query_v3.py`、`tests/services/test_rag_query_v4.py`：覆盖 v1-v4 Generation Token、v4 异步裁剪、空裁剪拒答及 `rag_return_top_n` 统计边界。
- `tests/api/test_rag.py`：覆盖 TokenMetrics 应用状态读取、v1-v4 单例注入和 v4 ContextTrimmer 组装。
- `tests/test_initialization.py`：覆盖 lifespan 初始化、状态挂载和关闭顺序。
- `docs/specs/16-上下文裁剪——Token 预算控制-spec.md`：同步 Embedding 原始 usage 提取，以及裁剪前按 `rag_return_top_n` 限制候选的实现语义。

### 删除文件

无。

### Diff 摘要

- `ContextTrimmer.trim(...)` 从同步占位方法升级为异步正式裁剪方法，输入输出仍为 `list[ChunkSearchHit]`。
- Context Token 只统计最终保留的 chunk 正文，不包含 System Prompt、用户问题、SourceBuilder 元数据前缀或生成答案。
- TokenMetrics 每次记录立即输出日志；正数同时写 Counter，并在当前用户上下文可用时使用 `HINCRBY rag:token-stats:{user_id}` 持久化累计值。
- v1-v4 统一从 LangChain AIMessage usage 记录 Generation Token；usage 不可用时只记录 unavailable 日志。
- OpenTelemetry 当前不配置 Exporter 或 Collector；指标失败和 Redis 失败均不改变查询结果。

### 关联文档同步

- 已同步本阶段 Spec：明确 Embedding 优先记录 provider `usage.total_tokens`，缺失时才记录 unavailable，并补充 `rag_return_top_n` 在 ContextTrimmer 前生效。
- Plan 的业务目标、范围和方案取舍未变化，因此未修改 Plan。
- 新增本 Process 文档记录实际代码和验证结果。

## 3.2 实际代码执行流程

1. FastAPI lifespan 初始化外部客户端；`enable_metrics=true` 时创建不带 Exporter 的 `MeterProvider`。
2. 应用创建单例 `TokenMetrics` 并写入 `app.state.token_metrics`，路由依赖从应用状态读取该实例。
3. 路由层先逐个执行知识库读权限校验；权限失败时不会进入检索、裁剪或指标记录。
4. v4 完成增强检索和 Reranker；真实精排成功时执行低置信度过滤，降级或跳过精排时保留 RRF 候选。
5. v4 按 `rag_return_top_n` 限制最终候选，然后调用 `await ContextTrimmer.trim(...)`。
6. ContextTrimmer 按相关性顺序累计 chunk 正文 token；预算内整条保留，首条超预算时截断，后续候选放不下时停止追加。
7. ContextTrimmer 输出裁剪摘要日志，并调用 `record_context_tokens(tokens=used_tokens)`；空候选或非正预算记录 0 后返回空列表。
8. 裁剪后无候选时返回固定拒答，不调用 SourceBuilder 或聊天模型；有候选时只用裁剪后的列表构建上下文和引用。
9. v1-v4 聊天模型响应返回后提取 completion/output token；存在真实 usage 时记录 Generation Token，不存在时记录 unavailable 日志。
10. TokenMetrics 对有效增量执行“即时 INFO 日志 -> OpenTelemetry Counter -> 当前用户 Redis Hash”；任一步失败都不向查询主流程抛出。
11. Embedding provider 调用通过 OpenAI-compatible SDK 取得原始响应；存在 `usage.total_tokens` 时记录真实 Embedding Token，缺失时记录 `embedding_token_usage_unavailable=true`，不估算账单 token。
12. 应用关闭时 shutdown MeterProvider，再关闭 Redis 等外部客户端。

与最初实现相比有两项一致性调整并已同步回 Spec：Embedding 改为从 OpenAI-compatible 原始响应读取 provider usage；v4 在 ContextTrimmer 前增加 `rag_return_top_n` 限制，避免统计未进入 Prompt 的候选正文。

## 3.3 核心代码片段及讲解

### 上下文贪心裁剪

```python
for hit in hits:
    chunk_tokens = self.count_tokens(hit.content)
    if used_tokens + chunk_tokens <= self.max_context_tokens:
        selected.append(hit)
        used_tokens += chunk_tokens
        continue

    if not selected:
        truncated_content = self.truncate_to_tokens(hit.content, self.max_context_tokens)
        if truncated_content:
            selected.append(replace(hit, content=truncated_content))
            used_tokens += self.count_tokens(truncated_content)
    break
```

候选已由前序 Reranker 或 RRF 排序，因此裁剪器不重新计算相关性。首条超预算时使用 `dataclasses.replace` 创建新对象，只替换正文并保留文档、知识库、页码、章节、chunk 和分数元数据。

### Token 指标双写与降级

```python
logger.info("[TokenMetrics] record_%s=%s", name, tokens)
counter.add(tokens, attributes)

user = current_user_var.get()
if user is not None:
    await self.redis.hincrby(
        f"rag:token-stats:{user.user_id}",
        redis_field,
        tokens,
    )
```

日志用于当前控制台观察，Counter 保留标准指标名和低基数属性，Redis 保存用户累计值。Counter 和 Redis 分别捕获异常，避免监控辅助链路影响回答生成。

### Generation usage 兼容提取

```python
usage_metadata = getattr(response, "usage_metadata", None)
if isinstance(usage_metadata, Mapping):
    tokens = usage_metadata.get("output_tokens")

response_metadata = getattr(response, "response_metadata", None)
token_usage = response_metadata.get("token_usage")
tokens = token_usage.get("completion_tokens")
```

优先使用 LangChain 标准 `usage_metadata.output_tokens`，再兼容 OpenAI 风格 `response_metadata.token_usage.completion_tokens`。两处都不存在或值非法时不伪造 token，只记录 unavailable。

### v4 统计口径对齐

```python
context_candidates = hits[: self.settings.rag_return_top_n]
trimmed_hits = await self.context_trimmer.trim(context_candidates)
context, sources = self.source_builder.build(trimmed_hits, return_top_n=...)
```

在裁剪前限制候选数量，使 ContextTrimmer 统计的正文、SourceBuilder 实际消费的候选和响应引用保持一致。

## 3.4 Review 关注点

- 请确认 `cl100k_base` 只作为本地工程近似，Context Token 不应被解释为阿里百炼真实账单 token。
- 请确认 `rag_return_top_n` 在 ContextTrimmer 前生效；同时注意 SourceBuilder 仍保留字符预算安全网，极端文本发生二次截断时，指标反映的是裁剪器输出正文而非二次截断后的字符数。
- 请确认首块截断只改变 `content`，不会丢失租户、知识库、文档和引用元数据。
- 请确认 TokenMetrics 的日志、Counter 或 Redis 异常不会中断检索、拒答、引用构建和回答生成。
- 请关注 OpenTelemetry 当前没有 MetricReader/Exporter，控制台可见的是即时业务日志，用户累计数据以 Redis Hash 为准。

验证结果：

- `uv run pytest -v`：182 passed，保留 1 条现有 Starlette/httpx 弃用警告。
- `uv run ruff check .`：All checks passed。
- `uv run mypy app`：仍有 13 个仓库既存错误，位于 `config.py`、`security.py`、`chunks.py`、`exception_handlers.py`、`reranker.py`、`markdown_parser.py` 和 `clients.py`；本次新增 Embedding usage 逻辑未新增 Mypy 错误。
