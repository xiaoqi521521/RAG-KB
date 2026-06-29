# Embedding 服务 Spec

## 背景与目标

文档切分层已经把解析后的文档拆成 `list[langchain_core.documents.Document]`，每个 `Document` 表示一个可入库、可检索、可溯源的 chunk。Embedding 服务负责把这些 chunk 正文批量转换为向量，并尽量复用缓存，降低索引延迟和模型调用成本。

参考 Spring AI 版本的核心目标是两个字：快和省。

- 快：缓存未命中的文本按批次调用 Embedding API，避免几百个 chunk 逐条请求。
- 省：相同文本、相同模型版本命中 Redis 缓存时直接返回向量，不重复消耗 Token。

Python 重构版使用 `langchain_openai.OpenAIEmbeddings` 接入 DashScope OpenAI-compatible Embedding API，使用 `redis.asyncio` 保存向量缓存。Embedding 服务只负责“文本到向量”，不负责文档解析、分块、入库、检索排序或权限过滤。

## In Scope

- 定义统一入口：`EmbeddingService`。
- 支持批量向量化：`embed_documents(texts)`。
- 支持单条查询向量化：`embed_query(text)`。
- 使用 Redis 做跨实例共享缓存。
- 缓存 key 基于模型版本和文本内容 hash 构造。
- 缓存命中时不调用外部 API。
- 缓存未命中时按批次调用 `OpenAIEmbeddings`。
- 返回向量顺序必须与输入文本顺序一一对应。
- 反序列化缓存失败时删除脏 key，并当作 cache miss 重新向量化。
- 对网络抖动、超时、5xx、限流等临时错误做有限重试和指数退避。
- 4xx 配置或参数错误不重试，直接抛给上层索引任务记录失败。
- 记录缓存命中数、API 调用耗时、批次数、文本数量、Token 消耗或不可用状态。
- 增加单元测试覆盖缓存命中、缓存 miss、批量顺序、脏缓存、重试边界和空输入。

## Out of Scope

- 不解析文档。
- 不切分文档。
- 不写入 `kb_doc_chunk`。
- 不更新 `kb_document` 或 `kb_index_task` 状态。
- 不实现 PGVector 检索。
- 不实现全文检索。
- 不实现 RRF、Reranker、上下文裁剪或回答生成。
- 不在本模块做权限过滤；权限过滤属于检索 SQL 和查询服务。
- 不引入本地进程内大容量向量缓存。
- 不返回零向量、随机向量或空向量作为失败兜底。

---

## 输入输出模型

### 批量输入

Embedding 服务面向索引管道的主入口接收文本列表：

```python
list[str]
```

调用方通常来自切分层输出：

```python
texts = [chunk.page_content for chunk in chunks]
vectors = await embedding_service.embed_documents(texts)
```

输入规则：

- `None` 不作为合法输入。
- 空列表返回空列表。
- 空白字符串默认跳过还是失败由上层索引服务决定；Embedding 服务建议对空白字符串抛出 `EmbeddingInputError`，避免生成无意义向量。
- 每条文本进入 API 前只做最小规范化：去掉首尾空白、统一换行为 `\n`。不做大小写转换、不改中文标点、不删除正文空格。

### 批量输出

输出为：

```python
list[list[float]]
```

输出规则：

- 返回数量必须等于输入数量。
- 第 `i` 个向量必须对应第 `i` 个输入文本。
- 向量维度必须与配置的 `embedding_dimension` 一致。
- 向量中只能包含可 JSON 序列化或可写入 PGVector 的有限浮点数。
- 如果任一未命中文本最终无法向量化，整个调用失败，不返回部分成功结果。

### 查询输入

在线查询链路使用单条入口：

```python
vector = await embedding_service.embed_query(question)
```

`embed_query` 与 `embed_documents([question])` 使用相同模型、相同缓存规则和相同向量维度，保证建库与查询处于同一向量空间。

---

## 配置

Embedding 配置必须与 LLM 配置解耦。LLM 使用 `OPENAI_API_KEY` / `OPENAI_BASE_URL`，Embedding 使用独立配置。

建议配置字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `embedding_api_key` | 空 | Embedding API Key，来自 `EMBEDDING_API_KEY` |
| `embedding_base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Embedding OpenAI-compatible endpoint |
| `embedding_model` | `text-embedding-v3` | 建库和查询必须一致 |
| `embedding_dimension` | `1024` | `text-embedding-v3` 当前入库维度，与 `VECTOR(1024)` 对齐 |
| `embedding_batch_size` | `10` | 单批文本数，降低单批超时和限流风险 |
| `embedding_cache_ttl_seconds` | `604800` | Redis 缓存 TTL，默认 7 天 |
| `embedding_cache_version` | `v1` | 缓存命名空间版本，模型或维度变化时递增 |
| `embedding_timeout_seconds` | `30` | 单批 API 超时时间 |
| `embedding_max_retries` | `3` | 临时错误最大尝试次数 |

当前项目已有：

```env
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_CACHE_TTL_SECONDS=604800
```

后续实现时如果补充 `embedding_dimension`、`embedding_batch_size`、`embedding_cache_version`、`embedding_timeout_seconds`、`embedding_max_retries`，应进入 `app.core.config.Settings`，不要在业务代码中硬编码。

---

## 模块设计

建议文件：

```plain
app/services/embedding.py
```

建议组件：

```plain
EmbeddingService
EmbeddingConfig
EmbeddingError
EmbeddingInputError
EmbeddingProviderError
EmbeddingCacheError
EmbeddingVectorCodec
```

### EmbeddingService

职责：

- 统一暴露批量和单条向量化入口。
- 处理文本规范化和输入校验。
- 查询 Redis 缓存。
- 收集缓存 miss 的文本并分批调用 Embedding API。
- 将 API 返回结果按原始输入顺序组装。
- 写入 Redis 缓存。
- 记录日志和指标。

建议接口：

```python
class EmbeddingService:
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...

    async def embed_query(self, text: str) -> list[float]:
        ...
```

### Embedding Client

底层客户端使用 `langchain_openai.OpenAIEmbeddings`。项目启动时由 `app.core.clients.get_embeddings()` 提供已初始化客户端，`EmbeddingService` 只依赖其抽象能力，不在业务方法内重新创建客户端。

调用约束：

- 批量文档优先使用异步批量方法，例如 `aembed_documents(batch)`。
- 查询文本使用 `aembed_query(text)` 或复用批量入口。
- 如果当前 LangChain 版本没有可用异步方法，再由实现层评估是否使用同步方法并放入线程池；不要在事件循环里直接执行长耗时同步请求。

### Redis Cache

缓存使用 `redis.asyncio.Redis`。

职责：

- 批量读取缓存。
- 写入向量字符串并设置 TTL。
- 删除反序列化失败的脏 key。

建议优先使用 pipeline 批量 `GET` / `SETEX`，减少 Redis 网络往返。Redis 命令本身失败时不应静默返回错误向量；可按场景选择：

- 读缓存失败：记录告警后当作全部 miss，继续调用 API。
- 写缓存失败：记录告警，不影响本次向量化结果返回。
- 删除脏 key 失败：记录告警，不阻断本次重新向量化。

---

## 缓存设计

### Key 结构

建议 key：

```plain
emb:{embedding_cache_version}:{md5(normalized_text)}
```

示例：

```plain
emb:v1:f0b8a2c7d0b4...
```

说明：

- 使用 MD5 是为了把长文本压缩成固定长度 key，避免把上千字 chunk 直接作为 Redis key。
- MD5 在这里不作为安全用途，只用于缓存定位；碰撞概率可接受。
- `embedding_cache_version` 用于模型、维度或序列化格式变化时整体切换缓存命名空间。
- key 中不再额外放 `embedding_model`，避免和 `embedding_cache_version` 表达重复；更换模型时必须同步递增缓存版本。
- 不把 `tenant_id` 放入 key。相同模型下相同文本的向量天然一致，缓存值只有向量、不包含原文和权限数据；权限隔离必须在检索层硬过滤，而不是靠缓存 key。

### Value 格式

建议使用 JSON 数组字符串：

```json
[0.0123, -0.0456, 0.0789]
```

选择 JSON 的原因：

- Python 标准库即可解析。
- 可读性高，便于排查。
- 不引入 Redis JSON 额外模块依赖。
- 比 CSV 更容易校验整体结构。

序列化规则：

- 只接受 `list[float]`。
- 写入前校验向量长度等于 `embedding_dimension`。
- 写入前校验每个元素为有限数值。

反序列化规则：

- JSON 解析失败，当作脏缓存。
- 解析结果不是 list，当作脏缓存。
- 维度不等于 `embedding_dimension`，当作脏缓存。
- 存在非数值、NaN 或 Infinity，当作脏缓存。
- 脏缓存删除后进入 API miss 流程。

---

## 批量流程

主流程：

```plain
输入 texts
  -> 校验并规范化文本
  -> 为每条文本构造 cache key
  -> 批量读取 Redis
  -> 命中且反序列化成功：记录 original_index -> vector
  -> 未命中或脏缓存：记录 original_index -> normalized_text
  -> 将 miss 文本按 embedding_batch_size 分批
  -> 调用 Embedding API
  -> 校验返回数量和维度
  -> 按 original_index 写回结果
  -> 将新向量写入 Redis，设置 TTL
  -> 按输入顺序组装 list[list[float]]
```

关键约束：

- 任何缓存命中、miss、分批调用都不能改变最终输出顺序。
- API 返回向量数量与 batch 文本数量不一致时，必须抛出 `EmbeddingProviderError`。
- 单批失败且重试耗尽后，整个 `embed_documents` 失败。
- 不允许用零向量填补失败项。
- 同一次调用中出现重复文本时，可以复用同一个缓存 key；实现可以先去重再调用 API，但最终必须恢复到原输入顺序。

### 批大小

默认 `embedding_batch_size=10`。

原因：

- DashScope 单次文本数限制之内保留更充足余量。
- 批太大容易触发限流或超时。
- 批太小会增加网络往返，批量收益下降。
- 10 在索引吞吐和请求稳定性之间更保守，适合作为默认值；后续可根据真实监控调整。

实现时应从配置读取批大小，并校验：

- `embedding_batch_size > 0`
- 不超过当前 provider 文档声明的单次输入上限

---

## 数据流转示例

以下示例用 3 个 chunk 展示一次索引中的向量化流程。为便于阅读，向量只写 4 维片段；真实 `text-embedding-v3` 应返回 1024 维。

### 输入 chunk

切分层输出：

```python
chunks = [
    Document(
        page_content="员工每年享有 5 天带薪年假。",
        metadata={
            "chunk_index": 0,
            "page_num": 2,
            "section_title": "休假制度",
            "estimated_tokens": 18,
        },
    ),
    Document(
        page_content="报销单据需在费用发生后 30 天内提交。",
        metadata={
            "chunk_index": 1,
            "page_num": 3,
            "section_title": "费用报销",
            "estimated_tokens": 22,
        },
    ),
    Document(
        page_content="员工每年享有 5 天带薪年假。",
        metadata={
            "chunk_index": 2,
            "page_num": 5,
            "section_title": "常见问题",
            "estimated_tokens": 18,
        },
    ),
]

texts = [chunk.page_content for chunk in chunks]
```

第 0 个和第 2 个 chunk 文本相同，因此会得到同一个缓存 key。

### Redis 读取

假设配置：

```plain
embedding_cache_version=v1
embedding_dimension=1024
embedding_batch_size=10
embedding_cache_ttl_seconds=604800
```

规范化文本并构造 key：

| original_index | normalized_text | Redis key | 读取结果 |
| --- | --- | --- | --- |
| 0 | `员工每年享有 5 天带薪年假。` | `emb:v1:8d9f...a21c` | 命中 |
| 1 | `报销单据需在费用发生后 30 天内提交。` | `emb:v1:19ab...77e0` | miss |
| 2 | `员工每年享有 5 天带薪年假。` | `emb:v1:8d9f...a21c` | 命中 |

Redis 中已存在的 value：

```json
[0.021, -0.034, 0.118, 0.006]
```

服务把 index 0 和 index 2 都记录为缓存命中，并只把 index 1 的文本加入 API miss 列表。

### API 调用与缓存写入

因为只有 1 条 miss，provider 只收到一批请求：

```python
await embeddings.aembed_documents([
    "报销单据需在费用发生后 30 天内提交。"
])
```

provider 返回：

```python
[
    [0.044, 0.013, -0.087, 0.229]
]
```

服务校验数量和维度后写入 Redis：

```plain
SETEX emb:v1:19ab...77e0 604800 "[0.044,0.013,-0.087,0.229]"
```

### 输出顺序恢复

最终返回给索引服务的向量仍按原始 chunk 顺序排列：

```python
vectors = [
    [0.021, -0.034, 0.118, 0.006],  # chunks[0]，来自缓存
    [0.044, 0.013, -0.087, 0.229],  # chunks[1]，来自 API
    [0.021, -0.034, 0.118, 0.006],  # chunks[2]，复用同一缓存
]
```

### 入库存储

索引服务再按相同下标把 chunk 和向量组装为数据库记录：

| 数据库字段 | 第 0 条记录示例 |
| --- | --- |
| `doc_id` | `101` |
| `kb_id` | `7` |
| `chunk_index` | `0` |
| `content` | `员工每年享有 5 天带薪年假。` |
| `embedding` | `[0.021, -0.034, 0.118, 0.006, ...]` |
| `page_num` | `2` |
| `section_title` | `休假制度` |
| `token_count` | `18` |
| `doc_version` | `3` |

注意：

- Redis 只缓存文本向量，不保存 `doc_id`、`kb_id`、页码、章节或权限信息。
- 数据库记录才保存业务归属和溯源字段。
- 检索时必须通过数据库的 `tenant_id` / `kb_id` / 权限列表做硬过滤，不能依赖 Embedding 缓存隔离权限。

---

## 错误处理与重试

### 异常类型

建议异常：

| 异常 | 说明 |
| --- | --- |
| `EmbeddingError` | Embedding 模块基类异常 |
| `EmbeddingInputError` | 输入为空白、类型不合法或参数非法 |
| `EmbeddingProviderError` | 外部 Embedding API 返回失败、数量不一致、维度不一致 |
| `EmbeddingCacheError` | 缓存序列化或 Redis 操作异常的封装；多数场景只记录不抛出 |

### 重试范围

应该重试：

- 网络连接错误。
- 读超时或连接超时。
- HTTP 5xx。
- 429 限流。
- provider SDK 标记为临时失败的异常。

不应重试：

- 400 参数错误。
- 401 / 403 鉴权错误。
- 404 模型或 endpoint 配置错误。
- 单条文本超过 provider 限制且不会因重试恢复。
- 返回向量维度不匹配。

### 重试策略

建议使用 `tenacity`：

```plain
最多尝试 embedding_max_retries 次
指数退避
开启 jitter
最终失败时 reraise 原始异常
```

指数退避的原因：

- 网络抖动可能短时间恢复。
- 限流窗口需要等待。
- jitter 可以降低多任务同时重试造成的集中冲击。

### 失败策略

Embedding API 最终失败时必须 fail loud：

- 不返回零向量。
- 不写入数据库。
- 不把失败项缓存。
- 抛出异常给索引任务服务，由上层更新任务状态、错误信息和可重试次数。

原因：零向量或脏向量进入 PGVector 后，后续检索可能随机召回无关 chunk，比显式索引失败更危险。

---

## Token 与监控

Embedding 服务需要记录以下指标或结构化日志字段：

| 字段 | 说明 |
| --- | --- |
| `text_count` | 本次输入文本数 |
| `cache_hits` | 缓存命中数量 |
| `cache_misses` | 缓存未命中数量 |
| `dirty_cache_count` | 脏缓存数量 |
| `api_batch_count` | API 批次数 |
| `api_elapsed_ms` | API 累计耗时 |
| `embedding_model` | 使用的模型 |
| `embedding_dimension` | 向量维度 |
| `token_count` | provider 返回时记录；不可用时记录 unavailable |
| `retry_count` | 临时错误重试次数 |

Token 统计规则：

- 如果 provider 返回 usage，则记录真实 token。
- 如果 LangChain / provider 当前不暴露 usage，则不要伪造精确值；记录 `token_usage_unavailable=true`，必要时另行记录基于字符数的估算值。
- 文档表 `kb_document.token_count` 可以由索引服务汇总本次真实 token 或估算 token；Embedding 服务只提供本次调用的统计结果。

---

## 与索引管道衔接

Embedding 服务位于文档切分之后、向量入库之前：

```plain
DocumentLoaderService
  -> ChunkService
  -> EmbeddingService
  -> Indexing / Repository
```

索引服务调用时应保证：

- 文本列表来自切分后 chunk 的 `page_content`。
- 返回向量数量与 chunk 数量一致。
- 第 `i` 个向量写入第 `i` 个 chunk。
- 入库字段 `embedding` 写入 `kb_doc_chunk.embedding`。
- 入库字段 `token_count` 可使用 chunk metadata 的估算 token；真实 embedding token 消耗汇总写入文档或任务统计。

Embedding 服务不直接接收 `tenant_id`、`kb_id`、`doc_id`。这些字段属于索引任务和数据库入库上下文，不能混入向量化缓存逻辑。

---

## 与查询管道衔接

在线查询时：

```plain
用户问题
  -> 查询改写
  -> EmbeddingService.embed_query(...)
  -> HybridRetriever 向量检索 + 全文检索
```

约束：

- 查询向量必须使用与建库相同的 `embedding_model` 和 `embedding_dimension`。
- 查询文本可以使用同一 Redis 缓存，以便重复问题降低延迟。
- 查询向量生成失败时，查询链路不能继续执行向量检索；应返回明确错误或降级到全文检索由后续查询服务单独定义。
- 权限过滤不在 Embedding 服务中实现。

---

## 验收标准

### 场景一：空输入

GIVEN 空列表

WHEN 调用 `embed_documents([])`

THEN 返回空列表，不访问 Redis，不调用 Embedding API。

### 场景二：全部缓存命中

GIVEN Redis 中已存在所有文本对应的合法向量

WHEN 调用 `embed_documents(texts)`

THEN 返回与输入顺序一致的向量列表，不调用 Embedding API。

### 场景三：部分缓存 miss

GIVEN 10 条文本中 6 条缓存命中、4 条未命中

WHEN 调用批量向量化

THEN 只向 provider 提交 4 条 miss 文本，并最终返回 10 条向量，顺序与原输入一致。

### 场景四：超过批大小

GIVEN `embedding_batch_size=10` 且有 25 条缓存 miss 文本

WHEN 调用批量向量化

THEN provider 被调用 3 批，批大小分别为 10、10、5。

### 场景五：脏缓存

GIVEN 某个 Redis value 不是合法 JSON 向量，或维度不是 1024

WHEN 调用批量向量化

THEN 删除该 key，按 cache miss 调用 API 重新生成向量，并写入新缓存。

### 场景六：API 返回数量不一致

GIVEN provider 对 3 条文本只返回 2 条向量

WHEN 调用批量向量化

THEN 抛出 `EmbeddingProviderError`，不返回部分结果。

### 场景七：临时错误重试

GIVEN provider 第一次调用超时，第二次调用成功

WHEN 调用批量向量化

THEN 服务按配置重试，最终返回向量，并记录 retry 指标。

### 场景八：4xx 不重试

GIVEN provider 返回 401 或 400

WHEN 调用批量向量化

THEN 直接抛出 `EmbeddingProviderError` 或透传可识别异常，不进行无意义重试。

### 场景九：失败不写零向量

GIVEN provider 重试耗尽仍失败

WHEN 调用批量向量化

THEN 抛出异常，不返回零向量，不写入缓存。

### 场景十：查询与建库同模型

GIVEN `embedding_model=text-embedding-v3` 且数据库字段为 `VECTOR(1024)`

WHEN 调用 `embed_query(...)`

THEN 返回 1024 维向量，后续可用于 PGVector 余弦检索。

---

## 测试建议

建议新增：

```plain
tests/services/test_embedding.py
```

优先使用 fake Redis 和 fake Embedding client 测试真实行为：

- 空输入不触发外部调用。
- 缓存命中路径。
- 部分 miss 路径。
- 输出顺序恢复。
- 重复文本只调用一次 provider 或至少返回一致结果。
- 脏缓存删除并重新生成。
- 批大小切分。
- provider 返回数量不一致。
- provider 返回维度不一致。
- 临时错误重试。
- 4xx 不重试。
- 写缓存失败不影响本次成功结果。

测试不应为了方便污染生产代码，例如加入仅测试使用的分支、参数或绕过逻辑。可以通过协议、fake client、fake cache 实现可测试性。

---

## 技术约束

- 使用 Python 3.12。
- 使用 `langchain_openai.OpenAIEmbeddings` 接入 OpenAI-compatible Embedding API。
- 使用 `redis.asyncio` 访问 Redis。
- 重试建议使用 `tenacity`，避免手写复杂重试循环。
- 建库和查询必须使用同一 `embedding_model` 和同一 `embedding_dimension`。
- `text-embedding-v3` 对应当前数据库 `VECTOR(1024)`。
- 缓存必须设置 TTL。
- 缓存 key 必须包含缓存版本信息；模型或维度变化时必须递增缓存版本。
- 不在 Embedding 服务里写数据库。
- 不在 Embedding 服务里做权限判断。
- 不用 Prompt 或 LLM 参与向量生成。
- API Key、base URL、模型名、批大小、TTL、重试参数必须可配置。

## 风险与取舍

- Redis 缓存可以跨实例共享，但向量 value 较大，需要关注内存容量和 TTL。
- JSON value 可读、易校验，但体积大于二进制格式；如果后续缓存规模很大，可评估 float32 二进制编码，但要同步提升版本号。
- 不把 `tenant_id` 放入缓存 key 可以提高命中率，但必须确保缓存只存向量、不存原文、不替代检索权限过滤。
- provider usage 可能不可用，Token 成本统计需要允许“不可用”状态，不能伪造精确消耗。
- API 失败时让索引任务失败会影响单次入库成功率，但能避免脏向量悄悄进入检索系统。
- 批大小过大可能限流，过小会变慢；默认 10 是偏稳定的初始值，后续应结合真实 DashScope 限制和监控指标调整。

## 参考资料

- `docs/references/09-Embedding 服务——批量向量化与缓存.md`
- LangChain `OpenAIEmbeddings` 官方 API Reference
- redis-py asyncio 官方示例
- Tenacity 官方文档
