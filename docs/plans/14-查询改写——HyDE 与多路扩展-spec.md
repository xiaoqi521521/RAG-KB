# 14-查询改写——HyDE 与多路扩展 Spec

## 前置自检

眼下最没有把握的事情是：第 14 章标题同时包含 HyDE 和多路扩展，但参考文献代码实际只把 HyDE 接入查询主链路，多路扩展只提供了方法和概念说明。

影响与处理：本 Spec 对齐参考文献实现方式：本阶段查询主链路只接入“原始问题混合检索 + HyDE 向量检索 + 二次 RRF 融合”。多路扩展只实现查询改写方法、缓存、清洗和测试，不接入增强检索，实际使用留到下一阶段。RRF 融合函数抽取为公共服务函数，由第 13 章混合检索和第 14 章增强检索共同复用，避免两处实现漂移。

文档核验：已按 `$find-docs` 要求通过 Context7 查询 LangChain Python 文档。当前文档确认聊天模型可接收 `SystemMessage` / `HumanMessage` 或等价 message 列表并返回消息对象；现有项目已使用 `langchain_core.messages` 和 `chat_model.ainvoke(...)`，本阶段沿用该调用形态，实际实现前仍以当前锁定依赖版本为准做一次本地测试。

## 2.1 任务背景

第 12 章已经建立基础 RAG 查询，第 13 章已经把单路向量检索升级为向量检索、全文检索和 RRF 融合。第 14 章要解决的是“用户问题表达”和“企业文档表达”不一致导致的召回不足。

本阶段在现有 `/api/v1/rag/query` 查询入口下新增查询改写增强管道：原始问题先走现有混合检索，HyDE 假设性回答只走向量检索，然后把两路排序结果再次 RRF 融合。多路扩展只实现方法，不参与本阶段查询主链路。所有检索仍在已授权 `kb_ids` 内执行，最终回答仍只基于真实召回 chunk，不允许把 HyDE 生成内容作为答案或引用。

技术前提：

- 查询入口已在路由层完成知识库读权限校验。
- `HybridRetriever` 已能对单个查询文本执行向量检索、全文检索和 RRF 融合，当前 RRF 函数仍在该文件内部。
- `RagQueryServiceV2` 已能基于检索结果构建上下文、调用聊天模型并返回 `RagQueryResponse`。
- Redis 客户端和 LangChain `ChatOpenAI` 客户端已在 `app/core/clients.py` 中统一初始化。

## 2.2 范围对齐

### In Scope

- 新增查询改写服务，负责 HyDE 生成、多路扩展方法、结果清洗、去重、缓存和降级。
- 新增增强检索服务，负责把原始问题混合检索结果与 HyDE 向量检索结果做二次 RRF 融合。
- 新增 `RagQueryServiceV3`，复用 `RagQueryServiceV2` 的生成、拒答和响应语义，替换检索来源为增强检索。
- 抽取公共 RRF 融合函数，让 `HybridRetriever` 和增强检索服务直接复用同一实现。
- 调整依赖组装，使 `rag_query_pipeline` 支持 `v3`。
- 增加单元测试和 API 测试，覆盖查询改写成功、失败降级、缓存不可用、权限短路、去重融合和拒答。

### Out of Scope

- 不新增 HTTP 路径或前端页面。
- 不把改写候选、HyDE 假设性回答或扩展问题加入 API 响应。
- 不把 HyDE 文本作为最终答案、引用来源或防幻觉兜底。
- 不实现 Reranker 精排、Reranker 超时降级或 Reranker 分数语义。
- 不实现精确 Token 预算裁剪、完整引用溯源、防幻觉二次校验、SSE 流式输出或多轮会话记忆。
- 不新增数据库表，不修改离线索引、分块、Embedding 入库或文档重建流程。
- 不做完整查询结果缓存。
- 不新增 `Settings` / 环境变量配置项；本阶段复用已有配置和模块内常量。
- 不把多路扩展问题接入实际检索链路；多路查询召回与融合放到下一阶段。

## 2.3 预计文件变更清单

新增文件：

- `app/services/rrf.py`：公共 RRF 融合函数，供混合检索和查询改写增强检索共同复用。
- `app/services/query_rewriter.py`：查询改写服务，封装 HyDE、多路扩展、缓存、清洗和降级。
- `app/services/enhanced_retriever.py`：查询改写增强检索服务，编排原始问题混合检索、HyDE 向量检索与二次 RRF 融合。
- `app/services/rag_query_v3.py`：查询改写版 RAG 查询服务，沿用 v2 的回答生成和拒答语义。
- `tests/services/test_rrf.py`：覆盖公共 RRF 排序、去重、分数累加和稳定排序。
- `tests/services/test_query_rewriter.py`：覆盖 HyDE、多路扩展、缓存、输出清洗和降级。
- `tests/services/test_enhanced_retriever.py`：覆盖原始混合检索与 HyDE 向量检索二次 RRF、HyDE 失败回退和统计字段。
- `tests/services/test_rag_query_v3.py`：覆盖 v3 查询服务的拒答、生成、hit_count 和异常处理。

修改文件：

- `app/services/hybrid_retriever.py`：删除文件内私有 RRF 实现，改为导入并复用 `app/services/rrf.py`。
- `app/api/routes/rag.py`：依赖组装支持 `rag_query_pipeline="v3"`，并注入查询改写和增强检索依赖。
- `tests/services/test_hybrid_retriever.py`：删除“RRF 模块不存在”的断言，改为验证 `HybridRetriever` 使用公共 RRF 后行为不变。
- `tests/api/test_rag.py`：补充依赖组装选择 v3，以及权限失败时不会调用查询服务。

预计不修改：

- `app/schemas/rag.py`：请求和响应结构保持不变，不暴露改写候选。
- `app/repositories/chunks.py`：继续由 `HybridRetriever` 复用现有向量和全文检索方法。
- `app/services/source_builder.py`：继续只接收最终排序后的 chunk 命中。

## 2.4 输入/输出模型

### HTTP 输入

沿用现有 `RagQueryRequest`：

```python
class RagQueryRequest(BaseModel):
    question: str
    kb_ids: list[int]
    session_id: str | None = None
```

校验规则保持不变：

- `question` 去除首尾空白后不能为空，最长 2000 字符。
- `kb_ids` 非空，最多 20 个，去重后必须均为正整数。
- 路由层在查询服务调用前逐个执行读权限校验。

### HTTP 输出

沿用现有 `RagQueryResponse`：

```python
class RagQueryResponse(BaseModel):
    answer: str
    sources: list[SourceCitation]
    hit_count: int
    latency_ms: int
```

输出规则：

- `answer` 只能来自真实召回 chunk 组装的上下文，或固定拒答文案。
- `sources` 只包含实际进入 Prompt 的引用 chunk。
- `hit_count` 与 `sources` 数量一致。
- 不新增 `rewritten_queries`、`hyde_answer` 或调试字段。

### 查询改写服务输入输出

新增内部模型：

```python
@dataclass(frozen=True)
class HydeRewriteResult:
    original_question: str
    hyde_answer: str | None
    used_cache: bool
    degraded_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MultiQueryRewriteResult:
    original_question: str
    expanded_queries: list[str]
    used_cache: bool
    degraded_reasons: tuple[str, ...]
```

输入：

- `question: str`：已由请求模型去空白的用户原始问题。

输出：

- `original_question` 必须等于标准化后的原始问题。
- `HydeRewriteResult.hyde_answer` 为可用的 HyDE 文本；失败时为 `None`。
- `MultiQueryRewriteResult.expanded_queries` 为清洗去重后的扩展问题，不包含原始问题。
- `degraded_reasons` 记录缓存或模型异常的降级原因，供日志使用。
- 本阶段增强检索只调用 HyDE 方法；多路扩展方法只供后续阶段接入和当前单元测试验证。

### 增强检索服务输入输出

新增内部模型：

```python
@dataclass(frozen=True)
class EnhancedRetrieveResult:
    hits: list[ChunkSearchHit]
    original_count: int
    hyde_count: int
    merged_count: int
    degraded_reasons: tuple[str, ...]
```

输入：

- `question: str`：用户原始问题。
- `kb_ids: list[int]`：已通过权限校验的知识库 ID。

输出：

- `hits` 为原始问题混合检索结果与 HyDE 向量检索结果二次融合后的候选。
- 各 count 字段用于日志和测试，不进入 HTTP 响应。
- `degraded_reasons` 传递 HyDE 生成、缓存或向量检索降级原因。

## 2.5 代码执行

### 2.5.1 总体调用链路

```python
query_rag(...)
  -> PermissionService.require_read(...)
  -> RagQueryServiceV3.query(...)
  -> EnhancedRetriever.retrieve(...)
  -> QueryRewriter.generate_hyde_answer(...)
  -> HybridRetriever.retrieve(question=original_question, kb_ids=kb_ids)
  -> EmbeddingService.embed_query(hyde_answer)                            # HyDE 可用时
  -> ChunkRepository.search_by_vector(query_vector=hyde_vector, ...)
  -> rrf_fuse(...)
  -> SourceBuilder.build(...)
  -> chat_model.ainvoke([SystemMessage(...), HumanMessage(...)])
  -> RagQueryResponse
```

### 2.5.2 查询改写服务

`QueryRewriter` 初始化依赖：

```python
class QueryRewriter:
    def __init__(
        self,
        *,
        chat_model: Any,
        redis_client: redis.Redis,
        chat_model_name: str,
        cache_ttl_seconds: int,
    ) -> None: ...
```

公开入口：

```python
async def generate_hyde_answer(self, question: str) -> HydeRewriteResult:
    """生成 HyDE 假设性回答；失败时只记录降级，不抛出用户可见异常。"""


async def expand_queries(self, question: str) -> MultiQueryRewriteResult:
    """生成多路扩展问题；本阶段只实现方法，暂不接入实际检索链路。"""
```

HyDE 执行顺序：

1. 标准化 `question`，并直接写入 `original_question`。
2. 读取 HyDE 缓存；缓存命中后直接使用。
3. HyDE 缓存未命中时，调用聊天模型生成 2-4 句假设性回答。
4. 清洗 HyDE 输出：去空白、限制最大长度、拒绝空结果。
5. 返回 `HydeRewriteResult`；缓存异常和模型异常只进入 `degraded_reasons`。

HyDE Prompt：

```text
请根据以下问题生成一个简洁的假设性回答，用于企业知识库检索。
要求：
1. 只写 2-4 句中文。
2. 保持原始问题意图，不扩展到无关主题。
3. 不要写“根据资料”“可能”“以下是”等前缀。
4. 这不是最终答案，只需要覆盖可能出现在文档中的表达。

问题：{question}
```

多路扩展 Prompt：

```text
请将以下问题改写成 3 个不同表达方式的企业知识库查询。
要求：
1. 保持原始意图不变。
2. 每个查询从不同角度表达，例如流程、条件、入口、限制或正式术语。
3. 每行一个查询，不要编号，不要解释。
4. 不要扩展到无关制度或更大主题。

原始问题：{question}
```

多路扩展方法执行顺序：

1. 标准化 `question`，并直接写入 `original_question`。
2. 读取多路扩展缓存；缓存命中后直接使用。
3. 多路扩展缓存未命中时，调用聊天模型生成 3 行扩展问题。
4. 清洗扩展输出：按行拆分、去编号、去空白、去重、去掉与原始问题相同的内容、限制数量。
5. 返回 `MultiQueryRewriteResult`；缓存异常和模型异常只进入 `degraded_reasons`。

本阶段 `EnhancedRetriever` 不调用 `expand_queries(...)`。该方法的行为、缓存和测试先对齐参考文献，为下一阶段接入多路查询召回做准备。

缓存 key 规则：

```python
hyde_key = f"rag:rewrite:v1:hyde:{chat_model_name}:{digest}"
multi_key = f"rag:rewrite:v1:multi:3:{chat_model_name}:{digest}"
```

其中 `digest` 使用标准化问题文本的哈希，`chat_model_name` 使用当前聊天模型名称。缓存 TTL 复用现有 `Settings.query_cache_ttl_seconds`。缓存 key 不包含 `kb_ids`，因为改写只基于用户问题，不读取知识库内容；检索权限仍在后续查询层硬过滤。

异常处理：

- Redis 读失败：记录 `hyde_cache_read_failed` 或 `multi_cache_read_failed`，继续调用模型。
- Redis 写失败：记录原因，不影响返回。
- 聊天模型失败：记录 `hyde_generation_failed` 或 `multi_generation_failed`，对应策略返回空。
- 聊天模型返回空：记录 `hyde_empty` 或 `multi_empty`。
- 输出超长：按模块内常量截断 HyDE；扩展问题逐条限制长度，超长条目丢弃。

模块内常量：

```python
HYDE_MAX_CHARS = 800
MULTI_QUERY_COUNT = 3
MULTI_QUERY_MAX_CHARS = 200
REWRITE_CACHE_VERSION = "v1"
```

这些常量不进入 `Settings`，如后续确实需要运行期调整，再单独发起配置设计。

### 2.5.3 公共 RRF 融合函数

新增 `app/services/rrf.py`：

```python
@dataclass(frozen=True)
class RrfSearchHit:
    hit: ChunkSearchHit
    score: float
    retrieval_sources: tuple[str, ...]


def rrf_fuse(
    ranked_results: Mapping[str, Sequence[ChunkSearchHit]],
    *,
    rrf_k: int,
) -> list[RrfSearchHit]: ...
```

行为要求：

1. `rrf_k <= 0` 时抛出 `ValueError`。
2. 每一路结果按传入顺序视为已排序，排名从 1 开始。
3. 同一 `chunk_id` 多路命中时只保留首次出现的 `ChunkSearchHit` 元数据。
4. 分数按 `1 / (rrf_k + rank)` 累加。
5. `retrieval_sources` 保留命中的通道名称，按首次命中顺序排列。
6. 最终按 RRF 分数降序排序，分数相同按首次出现顺序稳定排序。

`HybridRetriever` 调整：

- 删除当前文件内 `_RrfSearchHit` 和 `_rrf_fuse`。
- 从 `app.services.rrf` 导入 `rrf_fuse`。
- 原向量 + 全文融合继续传入 `{"vector": vector_hits, "fulltext": fulltext_hits}`。
- 输出 `ChunkSearchHit.score` 仍替换为公共 RRF 分数，保证 v2 响应语义不变。

### 2.5.4 增强检索服务

`EnhancedRetriever` 初始化依赖：

```python
class EnhancedRetriever:
    def __init__(
        self,
        *,
        query_rewriter: QueryRewriter,
        hybrid_retriever: HybridRetriever,
        embedding_service: EmbeddingService,
        chunk_repository: ChunkRepository,
        rrf_k: int,
        hyde_vector_top_k: int,
    ) -> None: ...
```

公开入口：

```python
async def retrieve(self, *, question: str, kb_ids: list[int]) -> EnhancedRetrieveResult:
    """对原始问题混合检索结果和 HyDE 向量检索结果做二次 RRF 融合。"""
```

执行顺序：

1. 调用 `HybridRetriever.retrieve(question=original_question, kb_ids=kb_ids)`，得到原始问题的混合检索结果。此结果内部已经完成向量检索、全文检索和第一阶段 RRF。
2. 调用 `query_rewriter.generate_hyde_answer(question)` 得到 HyDE 假设性回答。
3. HyDE 可用时，调用 `EmbeddingService.embed_query(hyde_answer)` 生成 HyDE 向量。
4. 使用 `ChunkRepository.search_by_vector(...)` 在同一 `kb_ids` 范围内执行 HyDE 向量检索。
5. 调用公共 `rrf_fuse(...)`，把 `original_hybrid` 和 `hyde_vector` 两路排序结果做第二阶段 RRF。
6. 返回 `EnhancedRetrieveResult`；后续进入 Prompt 的数量仍由 `SourceBuilder` 和现有 `rag_return_top_n` 控制。

第二阶段融合规则：

- `rank` 从 1 开始。
- `original_hybrid` 和 `hyde_vector` 各自按排名贡献 `1 / (rag_rrf_k + rank)`。
- 同一 `chunk_id` 多路命中时只保留一条，分数累加。
- 分数相同时按首次出现顺序稳定排序。
- 输出 `ChunkSearchHit.score` 替换为第二阶段 RRF 分数。

原始问题失败边界：

- 如果原始问题的 `HybridRetriever.retrieve(...)` 因 Embedding 或数据库异常失败，增强检索不吞掉异常。
- 如果 HyDE 生成失败、HyDE 向量化失败或 HyDE 向量检索失败，只降级 HyDE 路，最终使用 `original_hybrid` 结果继续回答。
- 多路扩展方法失败不影响本阶段查询主链路，因为本阶段不会调用 `expand_queries(...)`。

### 2.5.5 查询服务 v3

`RagQueryServiceV3` 可以直接复制 v2 主流程并替换 retriever 类型：

```python
class RagQueryServiceV3:
    def __init__(
        self,
        *,
        retriever: EnhancedRetriever,
        source_builder: SourceBuilder,
        chat_model: Any,
        settings: Settings,
    ) -> None: ...
```

`query(...)` 行为：

- 入参和 v2 一致：`question`、`kb_ids`、`user`。
- 无候选时返回 `RAG_REFUSAL_ANSWER`，不调用聊天模型。
- 有候选时复用 `SourceBuilder.build(...)`。
- 调用聊天模型时继续使用真实 chunk 上下文，不包含 HyDE 文本或多路扩展问题。
- 返回 `RagQueryResponse`，字段语义与 v2 完全一致。

异常处理与 v2 对齐：

- `EmbeddingError` 或聊天模型依赖未配置：返回 503。
- 数据库检索异常：返回 500。
- 生成模型异常或空结果：返回 503。
- HyDE 和缓存失败：不返回错误，只记录日志并降级；多路扩展不在本阶段查询主链路中调用。

### 2.5.6 依赖组装

本阶段不新增 `Settings` 字段。依赖组装只复用现有配置：

- `query_cache_ttl_seconds`：查询改写缓存 TTL。
- `chat_model`：缓存 key 中的模型标识。
- `rag_rrf_k`：公共 RRF 融合平滑参数。
- `rag_return_top_n`：最终进入 Prompt 和响应 sources 的 chunk 数量。
- `rag_vector_top_k` / `rag_fulltext_top_k`：继续由 `HybridRetriever` 控制单查询混合召回数量，其中 `rag_vector_top_k` 同时作为 HyDE 向量检索召回数量。

`app/api/routes/rag.py` 调整：

- `RagQueryPipeline` 协议保持不变。
- `get_rag_query_service(...)` 支持 `v3`。
- `v3` 组装顺序为：`EmbeddingService`、`ChunkRepository`、`HybridRetriever`、`QueryRewriter`、`EnhancedRetriever`、`RagQueryServiceV3`；其中 `QueryRewriter` 接收现有 `settings.chat_model` 和 `settings.query_cache_ttl_seconds`，`EnhancedRetriever` 接收现有 `settings.rag_rrf_k` 和 `settings.rag_vector_top_k`。
- `rag_query_pipeline` 允许值从 `v1/v2` 扩展为 `v1/v2/v3`。

### 2.5.7 事务边界和状态更新

本阶段不新增数据库写入，不开启新事务，不修改文档、chunk、会话或索引任务状态。

唯一持久化外部状态是 Redis 缓存：

- 缓存写入不参与数据库事务。
- 缓存失败不回滚查询流程。
- 缓存 value 只保存 HyDE 文本或扩展问题列表，不保存检索结果和引用内容；扩展问题缓存只在直接调用 `expand_queries(...)` 时使用。

## 2.6 技术约束与最佳实践

- 查询改写必须在权限校验之后执行，避免无权限请求触发模型和缓存调用。
- HyDE 只作为向量检索输入，不能进入回答 Prompt 的参考内容，避免模型假设污染可信上下文。
- 多路扩展本阶段只实现方法，不接入实际检索；后续接入前必须重新同步 Spec。
- 改写服务必须可降级，HyDE 或 Redis 失败不能改变原始问题混合检索的可用性。
- 扩展数量和输出长度使用模块内常量，缓存 TTL 复用现有查询缓存配置，避免为单阶段能力新增配置面。
- LangChain 调用方式沿用当前项目 `chat_model.ainvoke(messages)`，实现前需要用当前依赖版本跑最小单元测试确认返回对象含 `content`。

## 2.7 验收标准

### 场景一：权限失败时不触发查询改写

GIVEN 用户请求 `kb_ids=[2, 3]`，其中 `3` 无读权限  
WHEN 调用现有 RAG 查询接口  
THEN 返回 403，且不调用 `QueryRewriter`、`HybridRetriever`、Embedding、全文检索或聊天模型。

### 场景二：HyDE 成功参与检索但不进入答案

GIVEN HyDE 生成“新员工入职第一天需要报到和领取设备”，知识库召回真实 chunk  
WHEN 用户提问“新员工第一天要干嘛”  
THEN 系统先执行原始问题混合检索，再执行 HyDE 向量检索，并把两路结果二次 RRF 融合；最终 Prompt 只包含真实 chunk 内容，不包含 HyDE 文本。

### 场景三：多路扩展成功并去重

GIVEN 多路扩展返回三行，其中一行与原始问题相同  
WHEN 直接调用 `expand_queries(...)`  
THEN `expanded_queries` 不包含原始问题，数量不超过模块内常量 `MULTI_QUERY_COUNT=3`，且保持清洗后的原始顺序。

### 场景四：HyDE 失败降级

GIVEN HyDE 模型调用抛出异常  
WHEN 执行增强检索  
THEN 服务继续使用原始问题混合检索结果，响应不返回 5xx，日志记录 `hyde_generation_failed`。

### 场景五：多路扩展不进入本阶段查询主链路

GIVEN `expand_queries(...)` 可用，HyDE 正常  
WHEN 执行增强检索  
THEN 服务不调用 `expand_queries(...)`，只执行原始问题混合检索、HyDE 向量检索和二次 RRF。

### 场景六：HyDE 向量检索失败时回退原始问题

GIVEN HyDE 生成成功，但 HyDE 向量化或 HyDE 向量检索失败  
WHEN 执行增强检索  
THEN 服务继续使用原始问题混合检索结果，响应不返回 5xx，日志记录 HyDE 检索降级原因。

### 场景七：Redis 缓存读取失败不影响查询

GIVEN Redis `get` 调用失败  
WHEN 执行查询改写  
THEN 服务继续调用聊天模型生成改写结果，并记录缓存读取失败原因。

### 场景八：Redis 缓存写入失败不影响查询

GIVEN HyDE 生成成功但 Redis `setex` 调用失败  
WHEN 执行查询改写  
THEN 返回可用 HyDE 结果，查询主流程继续，日志记录缓存写入失败原因。

### 场景九：跨查询融合去重

GIVEN 原始问题混合检索结果和 HyDE 向量检索结果都召回同一个 `chunk_id=10`  
WHEN 增强检索融合结果  
THEN 最终 `hits` 中 `chunk_id=10` 只出现一次，且 RRF 分数为 `original_hybrid` 和 `hyde_vector` 两路排名贡献之和。

### 场景十：无候选时固定拒答

GIVEN 原始问题混合检索和 HyDE 向量检索都没有召回候选  
WHEN 调用 v3 查询服务  
THEN 返回固定拒答文案，`sources=[]`，`hit_count=0`，不调用聊天模型。

### 场景十一：响应字段保持兼容

GIVEN v3 查询成功召回 3 个候选，但 `rag_return_top_n=2`  
WHEN 返回查询响应  
THEN `sources` 长度为 2，`hit_count=2`，响应体不包含 HyDE 或扩展问题字段。

### 场景十二：依赖组装支持 v3

GIVEN `rag_query_pipeline="v3"`  
WHEN 调用 `get_rag_query_service(...)`  
THEN 返回 `RagQueryServiceV3` 实例；当配置为非 `v1/v2/v3` 时，返回明确配置错误。

### 场景十三：日志包含查询改写统计

GIVEN 查询改写启用，HyDE 命中缓存  
WHEN 查询完成  
THEN 日志包含 HyDE 缓存命中状态、原始混合检索数量、HyDE 向量检索数量、融合候选数量和总耗时，不输出完整 Prompt 或大段 chunk 内容。

### 场景十四：文档同步

GIVEN 本 Spec 后续进入实现阶段  
WHEN 代码实际文件清单、配置项或调用链路发生变化  
THEN 对应 Process 文档必须记录实际差异，必要时同步更新本 Spec。
