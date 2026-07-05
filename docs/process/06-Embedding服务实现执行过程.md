# Embedding 服务实现执行过程

## 执行范围

本次根据以下文件实现 Embedding 服务：

```plain
docs/specs/09-Embedding服务spec.md
docs/prompt/代码注释.md
```

In scope：

- 新增 Embedding 服务模块。
- 新增 Redis 向量缓存读写逻辑。
- 新增批量向量化和单条查询向量化入口。
- 新增向量 JSON 序列化 / 反序列化与维度校验。
- 新增缓存 key 生成逻辑：`emb:{embedding_cache_version}:{md5(normalized_text)}`。
- 新增 provider 调用重试逻辑。
- 新增 embedding 专属配置项。
- 更新 LangChain `OpenAIEmbeddings` 客户端初始化参数。
- 新增单元测试覆盖 spec 主要验收场景。

Out of scope：

- 不接入索引入库服务。
- 不写入 `kb_doc_chunk`。
- 不更新 `kb_document` 或 `kb_index_task` 状态。
- 不实现 PGVector 检索。
- 不实现全文检索、RRF、Reranker 或回答生成。
- 不在 Embedding 服务里做权限过滤。

## Context7 查询

按用户要求，使用 Context7 查询 LangChain 官方 Embedding 用法。

首次尝试命令：

```powershell
npx.cmd ctx7@latest resolve-library-id langchain-openai
```

结果：

```plain
error: too many arguments. Expected 0 arguments but got 2
```

随后查看 CLI 帮助：

```powershell
npx.cmd ctx7@latest --help
```

确认 Context7 CLI 查询文档的正确命令为：

```powershell
npx.cmd ctx7@latest library <name> <query>
npx.cmd ctx7@latest docs <libraryId> <query>
```

实际查询命令：

```powershell
npx.cmd ctx7@latest library langchain "OpenAIEmbeddings aembed_documents"
npx.cmd ctx7@latest docs /websites/langchain "OpenAIEmbeddings aembed_documents api_key base_url"
npx.cmd ctx7@latest docs /websites/langchain "OpenAIEmbeddings async aembed_documents aembed_query Python"
```

查询结论：

- LangChain Python 使用 `langchain_openai.OpenAIEmbeddings`。
- 官方示例支持初始化时传入 `model`、`base_url` 和 `api_key`。
- 异步 Embedding 方法为 `aembed_documents(...)` 和 `aembed_query(...)`。

本次实现据此使用 `OpenAIEmbeddings(..., model=..., api_key=..., base_url=..., timeout=..., check_embedding_ctx_length=False)` 初始化客户端，并在服务层调用 `aembed_documents(...)`。

## 新增和修改文件

新增文件：

```plain
app/services/embedding.py
tests/services/test_embedding.py
docs/process/06-Embedding服务实现执行过程.md
```

修改文件：

```plain
app/core/config.py
app/core/clients.py
.env
.env.example
```

## 配置变更

新增 `Settings` 字段：

```plain
embedding_api_key
embedding_base_url
embedding_dimension
embedding_batch_size
embedding_cache_version
embedding_timeout_seconds
embedding_max_retries
```

当前默认值：

```plain
EMBEDDING_DIMENSION=1024
EMBEDDING_BATCH_SIZE=10
EMBEDDING_CACHE_VERSION=v1
EMBEDDING_TIMEOUT_SECONDS=30
EMBEDDING_MAX_RETRIES=3
```

`app/core/clients.py` 调整为：

- LLM 使用 `OPENAI_API_KEY` / `OPENAI_BASE_URL`。
- Embedding 使用 `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL`。
- Embedding 客户端额外传入 `timeout=settings.embedding_timeout_seconds`。
- Embedding 客户端设置 `check_embedding_ctx_length=False`，确保 DashScope OpenAI-compatible endpoint 收到的是 `str` / `list[str]`，而不是 LangChain 自动 token 化后的 `list[list[int]]`。

## 核心实现

### EmbeddingConfig

使用不可变 dataclass 管理运行参数：

```python
@dataclass(frozen=True)
class EmbeddingConfig:
    dimension: int = 1024
    batch_size: int = 10
    cache_version: str = "v1"
    cache_ttl_seconds: int = 604800
    max_retries: int = 3
```

配置校验：

- `dimension > 0`
- `batch_size > 0`
- `cache_ttl_seconds > 0`
- `max_retries > 0`
- `cache_version` 非空

非法配置抛出 `EmbeddingInputError`。

### EmbeddingVectorCodec

职责：

- 将 `list[float]` 序列化为紧凑 JSON 字符串。
- 从 Redis value 反序列化向量。
- 校验向量维度等于 `embedding_dimension`。
- 校验向量元素是有限浮点数，拒绝 NaN / Infinity。

脏缓存处理：

- JSON 解析失败。
- value 不是 list。
- 维度不等于 1024。
- 元素不是数值。
- 元素为 NaN / Infinity。

这些场景统一抛出 `EmbeddingCacheError`，由 `EmbeddingService` 删除脏 key 并回退到 API 重新生成。

### EmbeddingService

对外入口：

```python
async def embed_documents(self, texts: list[str]) -> list[list[float]]
async def embed_query(self, text: str) -> list[float]
```

主要流程：

```plain
输入 texts
  -> 空列表直接返回 []
  -> 文本规范化
  -> 构造 Redis cache key
  -> 批量读取 Redis
  -> 命中：反序列化并按 original_index 保存
  -> miss / 脏缓存：收集唯一文本
  -> 按 embedding_batch_size 分批调用 provider
  -> 校验返回数量和向量维度
  -> 写回 Redis，设置 TTL
  -> 按原始输入顺序组装结果
```

### 缓存 key

实现使用：

```plain
emb:{embedding_cache_version}:{md5(normalized_text)}
```

说明：

- 不把 `embedding_model` 放进 key。
- 模型、维度或序列化格式变化时，通过递增 `embedding_cache_version` 失效旧缓存。
- 不把 `tenant_id` / `kb_id` 放进 key，因为缓存值只保存向量，不保存原文、业务归属或权限信息。

### 批处理

默认：

```plain
embedding_batch_size=10
```

实现按唯一 miss 文本分批调用：

```python
await embeddings.aembed_documents(batch_texts)
```

重复文本只需要生成一次向量，最终复制回所有相同文本的原始下标。

### 重试

使用 `tenacity.AsyncRetrying`：

```python
AsyncRetrying(
    stop=stop_after_attempt(max_retries),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_provider_error),
    reraise=True,
)
```

说明：

- 当前等待策略为固定指数退避，不启用 jitter。
- `multiplier=1, min=1, max=8` 保持 1 秒起步、8 秒封顶，更接近 Spring Retry 的固定指数级增长节奏。

当前可重试：

- `openai.APIConnectionError`
- `openai.APITimeoutError`
- `openai.RateLimitError`
- Python 内置 `TimeoutError`
- `httpx.TimeoutException`
- `httpx.TransportError`
- `openai.APIStatusError` 中的 429 或 5xx
- `httpx.HTTPStatusError` 中的 429 或 5xx

不重试：

- `EmbeddingProviderError`
- 400 / 401 / 403 / 404 等 4xx 配置或参数错误。
- provider 返回数量不一致。
- provider 返回维度不一致。

最终失败时抛出 `EmbeddingProviderError`，不会返回零向量。

## 测试过程

新增测试文件：

```plain
tests/services/test_embedding.py
```

测试使用：

- `FakeRedis`：模拟 Redis `pipeline().get().execute()`、`setex(...)`、`delete(...)`。
- `FakeEmbeddings`：模拟 LangChain `aembed_documents(...)` 和 `aembed_query(...)`。

覆盖场景：

- 空输入返回空列表。
- 空白输入抛出 `EmbeddingInputError`。
- 缓存命中时不调用 provider。
- 部分缓存 miss 时只提交 miss 文本。
- 重复文本复用同一个缓存 key，并恢复原始顺序。
- 脏缓存删除后回退 API 重新生成。
- 按配置批大小分批。
- `embed_query(...)` 复用批量流程。
- provider 返回数量不一致时抛出 `EmbeddingProviderError`。
- 超时类临时错误会重试。
- 重试等待策略使用固定指数退避，不使用 jitter。
- 非临时 provider 错误不重试。
- 写缓存失败不影响本次成功结果。
- `EmbeddingConfig.from_settings(...)` 读取项目配置。
- 非法 JSON 缓存抛出 `EmbeddingCacheError`。

## 验证记录

Embedding 服务测试：

```powershell
uv run python -m pytest tests/services/test_embedding.py -v
```

结果：

```plain
14 passed
```

初始化相关测试：

```powershell
uv run pytest tests/test_initialization.py -v
```

结果：

```plain
4 passed, 1 warning
```

warning 为 FastAPI / Starlette TestClient 对当前 `httpx` 适配的弃用提示，不是本次 Embedding 实现引入的失败。

## Review 关注点

- `EmbeddingService` 当前尚未接入索引入库流程；本次只实现独立服务层，符合 spec 范围。
- 缓存读取失败时整体降级为 miss，继续调用 provider；缓存写入失败只记录 warning，不影响本次返回。
- 脏缓存会删除并回退 API，避免旧版本或截断 value 阻断整批索引。
- provider 最终失败时不返回零向量，避免脏向量进入 PGVector。
- 当前 Token usage 记录为 unavailable；如果后续 provider 或 LangChain 暴露 usage，可在服务层扩展统计对象。
- `EmbeddingVectorCodec._validate_vector(...)` 当前作为内部校验方法使用；后续如果需要更强封装，可拆成公共 `validate(...)`。
- 测试文件为了避免 pytest 收集阶段路径问题，把 `app` 相关导入放在测试函数或 helper 内部。

## DashScope compatible 入参修订

后续重建索引联调时，Embedding 阶段出现 DashScope 400 错误：

```plain
InternalError.Algo.InvalidParameter: Value error, contents is neither str nor list of str.: input.contents
```

排查结论：

- `EmbeddingService` 传入 provider 前已经把 chunk 内容规范化为 `list[str]`。
- 当前 `langchain_openai.OpenAIEmbeddings` 默认 `check_embedding_ctx_length=True`。
- 该默认行为会先用 tiktoken 把文本转换为 token id 分片，再向 OpenAI-compatible endpoint 发送 `list[list[int]]`。
- OpenAI 原生 Embedding 接口可以接受 token id 输入，但 DashScope compatible embedding 只接受 `str` 或 `list[str]`，因此报 `input.contents` 类型非法。

修订方式：

```python
OpenAIEmbeddings(
    model=settings.embedding_model,
    api_key=embedding_api_key,
    base_url=settings.embedding_base_url,
    timeout=settings.embedding_timeout_seconds,
    check_embedding_ctx_length=False,
)
```

边界说明：

- 项目已经有文档分块层控制 chunk 长度，Embedding 客户端不再做 LangChain 内置 token id 分片。
- 如果后续遇到单个 chunk 超过 provider 限制，应调整 `chunk_size` / `overlap` 配置或在分块层拆分，而不是恢复 token id payload。

追加验证：

```powershell
$env:PYTHONPATH='.'; uv run pytest tests/test_initialization.py::test_init_clients_uses_separate_chat_and_embedding_openai_configs -v
$env:PYTHONPATH='.'; uv run pytest tests/services/test_embedding.py tests/services/test_indexing.py -v
```

结果：

```plain
1 passed, 1 warning
26 passed
```

最终验证：

```powershell
$env:PYTHONPATH='.'; uv run pytest -v
uv run ruff check app tests
$env:PYTHONPATH='.'; uv run mypy app/core/clients.py app/services/embedding.py
```

结果：

```plain
66 passed, 1 warning
All checks passed!
Found 3 errors in 1 file
```

mypy 未通过项集中在 `app/core/clients.py` 既有 LangChain 类型 stub 不匹配：`ChatOpenAI(max_tokens=...)`、`ChatOpenAI(api_key=str)`、`OpenAIEmbeddings(api_key=str)`。本次新增的 `check_embedding_ctx_length=False` 未引入新的 mypy 报错。
