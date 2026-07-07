# 12-基础 RAG 查询管道——检索与生成 Spec

本文基于 [12-基础 RAG 查询管道——检索与生成-plan.md](/D:/Code/python/Practical_Project/rag-kb/docs/plans/12-基础%20RAG%20查询管道——检索与生成-plan.md)、参考资料 [12-基础 RAG 查询管道——检索与生成.md](/D:/Code/python/Practical_Project/rag-kb/docs/references/12-基础%20RAG%20查询管道——检索与生成.md)，以及 [三阶段生成.md](/D:/Code/python/Practical_Project/rag-kb/docs/prompt/三阶段生成.md) 的 Spec 阶段规则，定义 Python / FastAPI / LangChain 版本的基础 RAG 查询管道技术方案。

本阶段只设计“向量检索 + Prompt 组装 + LLM 生成”的基础闭环。混合检索、RRF、Reranker、精确 Token 裁剪、流式输出、多轮对话和完整防幻觉评估不在本阶段实现。

## 2.0 Context7 官方用法核对

本次 Spec 完善已按 `$find-docs` 要求通过 Context7 查询 LangChain 官方文档：

```plain
ctx7 library langchain "Python ChatOpenAI async ainvoke messages system human RAG query pipeline"
ctx7 docs /websites/langchain "Python ChatOpenAI async ainvoke messages SystemMessage HumanMessage prompt template ChatPromptTemplate RAG question answering"
```

核对结论：

- 当前项目依赖 `langchain-openai`，聊天模型继续使用 `langchain_openai.ChatOpenAI`，与 `app/core/clients.py` 中的 `get_chat_model()` 保持一致。
- LangChain 官方示例支持在异步场景使用 `await chat_model.ainvoke(...)`；FastAPI async 路由和服务层不得改用阻塞式 `invoke(...)`。
- Chat 输入推荐使用 `langchain_core.messages.SystemMessage`、`HumanMessage` 等消息对象；也可以用 `ChatPromptTemplate` 组合提示词后再接模型。
- 本阶段推荐直接构造消息对象调用 `ainvoke`，不引入 `RunnablePassthrough`、`StrOutputParser` 或通用 RAG Chain，避免权限过滤、拒答和引用来源边界被 LangChain 抽象隐藏。
- Context7 结果来源包含 LangChain 官方文档 `https://docs.langchain.com/oss/python/integrations/chat/openai` 以及相关 chat input / prompt template 示例；实现阶段若锁定依赖版本升级，需要重新核对当前版本行为。

## 2.1 任务背景

前序阶段已经完成或规划了知识库管理、文档解析、分块、Embedding、PGVector 入库、文档重建和权限模型。基础在线查询阶段需要把已经入库的 chunk 用起来，为用户提供一个可调用的 HTTP 问答入口：

```plain
用户问题
  -> 读权限硬校验
  -> 问题向量化
  -> PGVector 向量召回
  -> 低召回拒答
  -> 参考内容与引用组装
  -> ChatOpenAI 生成答案
  -> ApiResponse 返回 answer 和 sources
```

当前项目可复用的技术前提：

```plain
app/services/embedding.py
  -> EmbeddingService.embed_query(text) 可复用批量向量化、Redis 缓存、重试和维度校验

app/models/kb.py
  -> DocChunk 已有 embedding、kb_id、doc_id、chunk_index、page_num、section_title、doc_version
  -> KbDocument 已有 status、version、file_name、is_deleted

app/services/permissions.py
  -> PermissionService.require_read(kb_id, user) 可做知识库读权限硬校验

app/core/clients.py
  -> get_chat_model() 返回已配置的 LangChain ChatOpenAI 客户端
  -> get_embeddings() 返回 OpenAI-compatible embedding 客户端

app/core/config.py
  -> Settings 已有 rag_vector_top_k、rag_return_top_n、rag_min_score、rag_context_max_tokens；rag_min_score 当前保留给后续阈值过滤规划
```

核心目标：

- 对外提供 `POST /api/v1/rag/query`。
- 检索前完成读权限校验，禁止无权限知识库进入向量查询。
- 检索 SQL 过滤当前文档版本、未删除文档和已完成索引文档。
- 没有召回时拒答，不调用聊天模型自由发挥；当前阶段不按相似度阈值过滤候选。
- 正常命中时，Prompt 只包含允许知识库的参考内容。
- 响应返回 answer、sources、hit_count、latency_ms，sources 可追溯到文档和 chunk。

## 2.2 范围对齐

### In Scope

本阶段技术交付物：

- 新增路由模块 `app/api/routes/rag.py`。
- 新增请求/响应模型 `app/schemas/rag.py`。
- 新增查询编排服务 `app/services/rag_query.py`。
- 新增引用组装模块 `app/services/source_builder.py`。
- 扩展 `app/repositories/chunks.py`，增加向量检索读方法。
- 更新 `app/api/router.py`，挂载 `/api/v1/rag` 路由。
- 基础查询只做 PGVector 向量检索，不做全文检索、RRF 或 Reranker。
- 查询入口必须校验所有请求 `kb_ids` 的读权限。
- 查询响应必须包含 answer、sources、hit_count、latency_ms。
- 增加单元测试和 API 测试，覆盖权限过滤、当前版本过滤、无召回拒答、低分候选仍可进入上下文、正常生成和来源元数据。

### Out of Scope

本阶段不做：

- 不实现 PostgreSQL 全文检索、混合检索和 RRF 融合。
- 不实现 HyDE、多路查询和查询改写。
- 不实现 Reranker 精排、超时控制和 RRF 降级。
- 不实现精确 Token 预算裁剪，只做基础 chunk 数量和上下文字符长度限制。
- 不实现答案事实性二次校验或完整引用溯源审计。
- 不实现 SSE 流式输出。
- 不实现多轮对话记忆、会话标题生成或历史消息拼接。
- 不新增或修改前端页面。
- 不修改索引管道、分块策略、Embedding 缓存 key、文档更新流程或数据库表结构。
- 不引入新的向量库、任务队列或缓存层。

## 2.3 输入/输出模型

### 2.3.1 查询接口

```plain
POST /api/v1/rag/query
Content-Type: application/json
```

请求体：

```json
{
  "question": "代码提交需要遵守什么规范？",
  "kb_ids": [2],
  "session_id": null
}
```

字段规则：

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `question` | string | 是 | 去除首尾空白后长度必须大于 0，最大长度建议 2000 字符 |
| `kb_ids` | list[int] | 是 | 至少 1 个；去重后逐个校验读权限；建议最多 20 个 |
| `session_id` | string \| null | 否 | 本阶段不使用多轮上下文；传入时只作为可选透传字段，暂不写会话表 |

建议 Pydantic DTO：

```python
class RagQueryRequest(BaseModel):
    """基础 RAG 查询请求体。"""

    question: str = Field(min_length=1, max_length=2000)
    kb_ids: list[int] = Field(min_length=1, max_length=20)
    session_id: str | None = None
```

实现要求：

- `question` 需要在校验或服务入口执行 `.strip()`，空白字符串返回 422 或 400。
- `kb_ids` 需要去重但保持用户传入的逻辑范围；去重后为空返回 422 或 400。
- 本阶段不校验 `session_id` 是否存在，不读取历史消息，不写入 `ChatSession` / `ChatMessage`。

### 2.3.2 成功响应

统一响应信封沿用 `ApiResponse`：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "answer": "代码提交需要先通过本地测试，并遵守提交信息和分支规范。[参考1]",
    "sources": [
      {
        "document_id": 1,
        "document_name": "研发规范.md",
        "kb_id": 2,
        "chunk_id": 10,
        "chunk_index": 3,
        "page_number": null,
        "section_title": "代码提交",
        "score": 0.82
      }
    ],
    "hit_count": 1,
    "latency_ms": 820
  }
}
```

字段语义说明：`hit_count` 表示实际进入 Prompt 的引用 chunk 数量，与 `sources.length` 一致。当前阶段不按 `rag_min_score` 过滤候选；`rag_return_top_n` 从 `.env` 的 `RAG_RETURN_TOP_N` 读取，控制最多进入 Prompt 和响应 `sources` 的 chunk 数量。

建议响应 DTO：

```python
class SourceCitation(BaseModel):
    """回答引用来源，供前端展示和后续溯源。"""

    document_id: int
    document_name: str
    kb_id: int
    chunk_id: int
    chunk_index: int
    page_number: int | None
    section_title: str | None
    score: float


class RagQueryResponse(BaseModel):
    """基础 RAG 查询响应体。"""

    answer: str
    sources: list[SourceCitation]
    hit_count: int
    latency_ms: int
```

### 2.3.3 拒答响应

无召回统一返回 200，业务上是一次成功处理的查询，只是答案为拒答：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "answer": "在知识库中未找到相关内容。请确认问题是否与所选知识库相关，或尝试换一种问法。",
    "sources": [],
    "hit_count": 0,
    "latency_ms": 120
  }
}
```

当前阶段不启用低置信度阈值过滤；只在检索无召回时返回空 `sources`。相似度阈值过滤将在后续阶段重新设计并补充验收标准。

### 2.3.4 错误响应

| HTTP 状态码 | 场景 | 行为 |
| --- | --- | --- |
| 401 | 未认证 | 由现有认证依赖处理 |
| 403 | 任一 `kb_id` 无读权限 | 不执行向量化、检索或模型调用 |
| 404 | 任一 `kb_id` 不存在或已删除 | 由 `PermissionService.require_read` 返回 |
| 422 或 400 | `question` 空、`kb_ids` 为空或格式非法 | 不进入服务层查询 |
| 503 | Embedding provider 不可用或聊天模型不可用 | 记录异常；不返回伪答案 |
| 500 | 未预期异常 | 交给统一异常处理，日志记录 request 关键上下文 |

本阶段不新增持久化结果。`session_id` 不触发 `ChatMessage` 写入，因此查询成功不会修改数据库业务状态。

## 2.4 代码执行流程

### 2.4.1 总体调用链路

```plain
rag.query route
  -> get_current_user
  -> get_permission_service
  -> get_rag_query_service
  -> RagQueryRequest 校验
  -> for kb_id in unique_kb_ids: PermissionService.require_read(kb_id, user)
  -> RagQueryService.query(question, unique_kb_ids, user)
  -> EmbeddingService.embed_query(question)
  -> ChunkRepository.search_by_vector(query_vector, kb_ids, top_k)
  -> SourceBuilder.build(hits, return_top_n)
  -> ChatOpenAI.ainvoke([SystemMessage(...), HumanMessage(...)])
  -> RagQueryResponse
  -> ApiResponse.ok(response)
```

### 2.4.2 路由模块

新增文件：

```plain
app/api/routes/rag.py
```

职责：

- 声明 `APIRouter()`。
- 提供 `POST /query` 路由。
- 注入当前用户、权限服务和 RAG 查询服务。
- 在路由层完成所有 `kb_ids` 的读权限校验。
- 不直接拼 SQL、不直接调用 LLM、不直接组装 Prompt。

建议 route 函数：

```python
@router.post("/query")
async def query_rag(
    request: RagQueryRequest,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    rag_service: RagQueryService = Depends(get_rag_query_service),
) -> ApiResponse[RagQueryResponse]:
    """执行基础 RAG 查询。

    Args:
        request: 用户问题、目标知识库列表和可选会话 ID。
        user: 当前认证用户上下文。
        permission_service: 知识库权限服务，用于检索前硬校验读权限。
        rag_service: 查询编排服务，用于完成向量检索和回答生成。

    Returns:
        包含回答、引用来源、命中数量和总耗时的统一响应。
    """
```

路由挂载：

```python
from app.api.routes import rag

api_router.include_router(rag.router, prefix="/rag", tags=["rag"])
```

### 2.4.3 依赖组装

可在 `app/api/routes/rag.py` 内部定义 `get_rag_query_service(...)`，也可以后续迁移到公共依赖模块。为保持局部性，本阶段建议先放在路由模块内：

```plain
get_rag_query_service(session, settings)
  -> ChunkRepository(session)
  -> EmbeddingService(get_embeddings(), get_redis(), EmbeddingConfig(...))
  -> get_chat_model()
  -> SourceBuilder(max_context_chars=settings.rag_context_max_tokens * 4)
  -> RagQueryService(...)
```

注意：

- `ChatOpenAI` 客户端由 `get_chat_model()` 获取，未配置时会抛出 `RuntimeError`，服务层应转为 503。
- `EmbeddingService` 的构造函数当前为 `EmbeddingService(embeddings, redis_client, config=None)`；实现阶段可以沿用默认 `EmbeddingConfig()`，也可以显式传入由 `Settings` 对齐的配置。
- 如果显式传入 `EmbeddingConfig`，必须至少保证 `dimension=settings.embedding_dimension`、`batch_size=settings.embedding_batch_size`、`cache_version=settings.embedding_cache_version`、`cache_ttl_seconds=settings.embedding_cache_ttl_seconds`、`max_retries=settings.embedding_max_retries` 与当前配置一致，避免查询向量维度或缓存隔离和索引阶段不一致。
- `get_rag_query_service(...)` 只负责组装依赖，不在依赖函数中执行权限校验、向量化、检索或模型调用。

### 2.4.4 ChunkRepository 向量检索

修改文件：

```plain
app/repositories/chunks.py
```

新增轻量结果对象：

```python
@dataclass(frozen=True)
class ChunkSearchHit:
    """向量检索命中的 chunk 及其来源信息。"""

    chunk_id: int
    doc_id: int
    document_name: str
    kb_id: int
    chunk_index: int
    content: str
    page_num: int | None
    section_title: str | None
    score: float
```

新增方法：

```python
async def search_by_vector(
    self,
    *,
    query_vector: list[float],
    kb_ids: list[int],
    top_k: int,
) -> list[ChunkSearchHit]
```

查询约束：

```plain
DocChunk.kb_id IN :kb_ids
DocChunk.doc_id = KbDocument.id
DocChunk.doc_version = KbDocument.version
KbDocument.status = "DONE"
KbDocument.is_deleted = false
ORDER BY DocChunk.embedding <=> query_vector
LIMIT top_k
```

分数规则：

- Repository 内部可以先取 PGVector 距离 `distance`。
- 对外返回 `score = 1 / (1 + distance)`，范围为 `(0, 1]`，当前仅作为排序和展示分数。
- `rag_min_score` 默认值来自配置，当前阶段不参与候选过滤。
- 如果后续切换为 cosine similarity 或数据库表达式变化，必须同步更新本 Spec 和测试断言。

实现建议：

- 优先使用 SQLAlchemy `select(...)` 和 `join(...)`，必要时通过 `DocChunk.embedding.op("<=>")(query_vector)` 表达 PGVector 距离。
- 查询向量必须是 `list[float]`，不能传字符串拼接，避免 SQL 注入和维度错误隐藏。
- `top_k` 使用 `settings.rag_vector_top_k`，但最终给模型的上下文数量使用 `settings.rag_return_top_n` 截断。
- 跨多个知识库查询时应在单条 SQL 中对允许的 `kb_ids` 做全局排序和 `LIMIT top_k`，不要对每个知识库分别取 TopK 后在 Python 层简单截断。
- Repository 返回前应显式计算 `distance` 和 `score`，但对服务层只暴露 `score`；原始距离如需调试可写 debug 日志，不进入 API 响应。
- 具体 SQLAlchemy / pgvector 表达式在实现阶段必须通过当前依赖和测试验证；本 Spec 只固定业务约束、排序语义和分数换算，不允许手写拼接向量字面量绕过参数化。

### 2.4.5 SourceBuilder

新增文件：

```plain
app/services/source_builder.py
```

职责：

- 将 `ChunkSearchHit` 列表转换为 Prompt 上下文文本。
- 将命中结果转换为 `SourceCitation`。
- 控制基础上下文长度，避免无限拼接。

建议方法：

```python
class SourceBuilder:
    """构建 RAG 生成阶段使用的参考上下文和引用来源。"""

    def __init__(self, *, max_context_chars: int) -> None: ...

    def build(
        self,
        hits: list[ChunkSearchHit],
        *,
        return_top_n: int,
    ) -> tuple[str, list[SourceCitation]]:
        """将检索命中转换为上下文文本和引用来源。"""
```

上下文格式：

```plain
[参考1]
文档：研发规范.md
知识库ID：2
ChunkID：10
页码：不适用
章节：代码提交
内容：
代码提交前必须通过本地测试...
```

长度控制：

- 先按检索顺序截取前 `rag_return_top_n` 个 hit。
- 再按 `settings.rag_context_max_tokens * 4` 作为近似字符预算裁剪上下文。
- 如果单个 chunk 超出剩余预算，截断该 chunk 内容并停止追加。
- 精确 token 预算在第 16 章实现，本阶段不得引入复杂 tokenizer 依赖或改写分块逻辑。

### 2.4.6 RagQueryService

新增文件：

```plain
app/services/rag_query.py
```

职责：

- 编排基础查询主链路。
- 不直接做权限判断，权限由路由入口完成；服务仍只接收已授权 `kb_ids`。
- 负责无召回、模型失败和耗时日志。
- 不写会话表。

建议构造参数：

```python
class RagQueryService:
    """基础 RAG 查询服务，编排向量检索、上下文构建和模型生成。"""

    def __init__(
        self,
        *,
        embedding_service: EmbeddingService,
        chunk_repository: ChunkRepository,
        source_builder: SourceBuilder,
        chat_model: ChatOpenAI,
        settings: Settings,
    ) -> None: ...
```

建议核心方法：

```python
async def query(
    self,
    *,
    question: str,
    kb_ids: list[int],
    user: CurrentUser,
) -> RagQueryResponse:
    """执行基础 RAG 查询并返回回答、引用和耗时统计。"""
```

执行细节：

```plain
1. started_at = perf_counter()
2. normalized_question = question.strip()
3. query_vector = await embedding_service.embed_query(normalized_question)
4. hits = await chunk_repository.search_by_vector(
       query_vector=query_vector,
       kb_ids=kb_ids,
       top_k=settings.rag_vector_top_k,
   )
5. if not hits: return refusal_response(...)
6. context, sources = source_builder.build(hits, return_top_n=settings.rag_return_top_n)
7. answer = await generate_answer(normalized_question, context)
8. return RagQueryResponse(answer=answer, sources=sources, hit_count=len(sources), latency_ms=...)
```

拒答文案固定为：

```plain
在知识库中未找到相关内容。请确认问题是否与所选知识库相关，或尝试换一种问法。
```

### 2.4.7 Prompt 与 ChatOpenAI 调用

LangChain 官方文档确认 `langchain_openai.ChatOpenAI` 支持异步 `ainvoke(...)`；官方 chat input 示例使用 `SystemMessage` 和 `HumanMessage` 作为消息对象。当前项目的 `get_chat_model()` 已返回 `ChatOpenAI`，FastAPI async 服务层必须使用 `await chat_model.ainvoke(messages)`，不得在请求链路中调用同步 `invoke(...)`。

推荐消息结构：

```python
from langchain_core.messages import HumanMessage, SystemMessage

messages = [
    SystemMessage(content=system_prompt),
    HumanMessage(content=normalized_question),
]

response = await chat_model.ainvoke(messages)
```

可选实现方式：

```python
from langchain_core.prompts import ChatPromptTemplate

prompt = ChatPromptTemplate(
    [
        ("system", system_prompt_template),
        ("human", "{question}"),
    ]
)
messages = prompt.format_messages(context=context, question=normalized_question)
response = await chat_model.ainvoke(messages)
```

本阶段二选一即可。推荐优先使用 `SystemMessage` / `HumanMessage` 的直接写法，因为 RAG 编排、拒答和引用组装已经由 `RagQueryService` 显式完成，不需要为了两条消息引入额外 Chain。

System Prompt：

```plain
你是企业内部知识库的智能助手。你的任务是只根据【参考内容】回答员工问题。

重要规则：
1. 只能根据【参考内容】回答，不要使用通用知识、经验或猜测补充公司制度。
2. 如果参考内容不足以回答，必须明确回答“在知识库中未找到相关内容”。
3. 回答使用中文，尽量准确、简洁。
4. 如果答案综合了多个参考片段，需要在关键句后标注参考编号，例如 [参考1]。
5. 禁止编造参考内容中不存在的流程、数字、政策、负责人或时间。

【参考内容】
---
{context}
---
```

回答内容提取：

- `ainvoke(...)` 返回 `AIMessage`。
- 使用 `response.content` 作为答案正文；正常情况下应为字符串。
- 如果 `response.content` 是 list、dict、空字符串或全空白字符串，返回 503 并记录异常，不返回空答案。
- 模型输出中的引用编号要求由 Prompt 约束；本阶段不对 `[参考N]` 做后置强校验，也不因为缺少引用编号而二次调用模型。

### 2.4.8 异常处理

Embedding 失败：

```plain
EmbeddingInputError
  -> 400 或 422，通常由请求校验提前拦截

EmbeddingProviderError / RuntimeError
  -> 503
  -> 记录 user_id、kb_ids、错误类型和 embedding_elapsed_ms
```

检索失败：

```plain
SQLAlchemyError / DB 连接异常
  -> 500
  -> 不调用聊天模型
```

聊天模型失败：

```plain
chat_model.ainvoke(...) 抛出异常
  -> 503
  -> 不返回只由检索片段拼成的伪答案
```

日志要求：

- 查询开始和结束记录 `user_id`、`kb_ids`、`hit_count`、`latency_ms`。
- 拒答日志记录 `reason=no_hits`。
- 模型异常日志记录错误类型，不记录完整 Prompt 内容，避免日志泄露内部文档大段内容。

## 2.5 技术约束与最佳实践

- 权限必须在检索前通过 `PermissionService.require_read(...)` 完成，不能依赖 Prompt 或前端传参。
- 如果请求包含多个 `kb_id`，任一知识库无权限时整个请求失败，不静默过滤后继续查询。
- 检索 Repository 必须 join `KbDocument`，过滤 `doc_version = KbDocument.version`，避免召回重建过程中的半成品 chunk。
- 检索 Repository 必须过滤 `KbDocument.status == DocumentStatus.DONE.value` 和 `KbDocument.is_deleted.is_(False)`。
- 本阶段不使用 LangChain 一行式 RetrievalQA、Agent 或 Tool 抽象承接主链路，避免权限、拒答和引用溯源边界被隐藏。
- 本阶段不使用 LangChain 官方 RAG Chain 示例中的 `retriever | prompt | model | parser` 管道作为主链路；该模式可用于理解调用方式，但不适合承载本项目的权限硬过滤、版本过滤和 sources 结构化返回。
- `EmbeddingService.embed_query(...)` 与索引阶段必须使用同一 embedding 模型和维度。
- 向量查询必须使用参数化表达式或 SQLAlchemy 表达式，不手写拼接向量字符串。
- `rag_vector_top_k` 控制数据库召回数量，`rag_return_top_n` 控制进入 Prompt 的 chunk 数量，两者不能混用。
- 当前阶段不使用 `rag_min_score` 过滤候选；该配置保留给后续相似度阈值过滤方案。
- Prompt 中不得包含无权限知识库的任何 chunk 内容。
- 本阶段不把 `session_id` 写入会话表，避免提前进入多轮对话范围。
- 所有新增函数、方法和核心类必须按项目约定添加 Docstring；复杂 SQL 和拒答边界需要添加说明“为什么这么过滤”。
- 不新增数据库迁移；若实现时发现现有字段无法支持当前版本过滤，必须先同步更新 Spec，再设计迁移。
- 本次 Context7 核对只覆盖 LangChain ChatOpenAI、chat messages 和 prompt template 用法；涉及 SQLAlchemy、pgvector、FastAPI 或 Pydantic 的新 API 若在实现阶段新增示例或改动，必须再查对应官方文档或当前版本源码。

## 2.6 验收标准

### 功能行为

- `POST /api/v1/rag/query` 接收合法 `question` 和 `kb_ids` 后返回 `ApiResponse[RagQueryResponse]`。
- 正常命中时返回非空 `answer`、非空 `sources`、`hit_count > 0` 和 `latency_ms >= 0`。
- 正常命中时 Prompt 中只包含检索命中的参考内容，不包含无关知识库 chunk。
- 无召回时返回固定拒答文案，`sources=[]`，`hit_count=0`，且不调用聊天模型。
- 正常命中时，即使候选分数低于 `settings.rag_min_score`，当前阶段也允许进入 `SourceBuilder`，最终数量由 `rag_return_top_n` 和上下文预算控制。
- `session_id` 传入时不影响本阶段查询结果，也不读取或写入会话历史。

### 异常分支

- 空白 `question` 返回请求校验错误，不调用 Embedding。
- 空 `kb_ids` 返回请求校验错误，不调用权限服务以后的查询链路。
- 任一 `kb_id` 无读权限时返回 403，且不调用 Embedding、ChunkRepository 或聊天模型。
- Embedding provider 失败时返回 503，不执行检索和生成。
- 检索 SQL 失败时返回 500，不调用聊天模型。
- 聊天模型失败时返回 503，不返回拼接 chunk 作为答案。

### 权限与数据边界

- API 测试必须证明无权限知识库不会进入 `ChunkRepository.search_by_vector(...)`。
- Repository 测试必须证明检索条件包含：
  - `DocChunk.kb_id` 在允许列表内。
  - `DocChunk.doc_version == KbDocument.version`。
  - `KbDocument.status == DONE`。
  - `KbDocument.is_deleted == False`。
- 重建中的新版本 chunk 即使已经写入，也不能在 `KbDocument.version` 切换前被召回。
- 已删除文档和 FAILED 文档的 chunk 不参与查询。

### 引用与 Prompt

- `SourceCitation.document_id` 等于命中文档 ID。
- `SourceCitation.document_name` 来自 `KbDocument.file_name`。
- `SourceCitation.kb_id`、`chunk_id`、`chunk_index`、`page_number`、`section_title` 来自命中 chunk。
- sources 顺序与进入 Prompt 的参考编号一致。
- Prompt 中每个参考片段都带 `[参考N]` 编号。
- 模型回答要求标注 `[参考N]`，但本阶段不做回答后置强校验。

### 日志与可观测性

- 正常查询日志包含 `user_id`、`kb_ids`、`hit_count`、`latency_ms`。
- Embedding、retrieval、generation 三段耗时至少在 debug 或 info 日志中可见。
- 日志不输出完整上下文和完整 Prompt。
- 本阶段不强制新增 Prometheus 指标；如实现新增指标，必须同步更新本 Spec。

### 测试命令

实现完成后至少运行：

```powershell
uv run pytest tests/services/test_rag_query.py -v
uv run pytest tests/services/test_source_builder.py -v
uv run pytest tests/repositories/test_chunks.py -v
uv run pytest tests/api/test_rag.py -v
uv run pytest tests/services/test_permissions.py -v
```

若实现只新增文档而未写代码，不运行上述测试；但必须重新读取 Spec 文件确认落盘，并检查占位符和命名后缀。

### 文档同步

- 本文件名必须保持 `*-spec.md` 后缀：`12-基础 RAG 查询管道——检索与生成-spec.md`。
- 对应 Plan 已存在：`docs/plans/12-基础 RAG 查询管道——检索与生成-plan.md`。
- 代码实现完成后，需要新增对应 Process 文档，说明实际文件变更、最终执行流程和核心代码片段。
- 如果实现阶段决定写入 `ChatSession` / `ChatMessage`，必须先同步更新本 Spec，因为当前范围明确不写会话表。
- 如果实现阶段提前引入全文检索、RRF 或 Reranker，必须同步更新本 Spec，并说明为什么第 13-15 章能力被前置。
