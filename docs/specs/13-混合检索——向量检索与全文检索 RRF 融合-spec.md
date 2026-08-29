# 13-混合检索——向量检索与全文检索 RRF 融合 Spec

本文基于 [13-混合检索——向量检索与全文检索 RRF 融合-plan.md](/D:/Code/python/Practical_Project/rag-kb/docs/plans/13-混合检索——向量检索与全文检索%20RRF%20融合-plan.md)、参考资料 [13-混合检索——向量检索与全文检索 RRF 融合.md](/D:/Code/python/Practical_Project/rag-kb/docs/references/13-混合检索——向量检索与全文检索%20RRF%20融合.md)，以及 [三阶段生成.md](/D:/Code/python/Practical_Project/rag-kb/docs/prompt/三阶段生成.md) 的 Spec 阶段规则，定义 Python / FastAPI / LangChain 版本的混合检索技术方案。

本阶段只升级在线查询管道中的检索层：从单路 PGVector 向量检索升级为“向量检索 + PostgreSQL 全文检索 + RRF 融合”。接口路径、路由层权限入口、Prompt 生成、SourceBuilder 和 ChatOpenAI 调用保持第 12 章基础查询管道的主体形态。混合检索内部的第二层权限范围过滤由后续 [检索层权限过滤 Spec](../../.scratch/hybrid-retrieval-permission-filter/spec.md) 和 [ADR-0007](../adr/0007-filter-retrieval-scope-inside-hybrid-retriever.md) 定义；Reranker、HyDE、多路查询、精确 token 裁剪、流式输出和多轮对话不在本阶段实现。

## 2.0 Context7 官方用法核对

本次 Spec 按 `$find-docs` 要求通过 Context7 查询 PostgreSQL 官方文档，并解析 SQLAlchemy 文档入口：

```plain
npx.cmd ctx7@latest library postgresql "PostgreSQL full text search to_tsvector plainto_tsquery websearch_to_tsquery ts_rank"
npx.cmd ctx7@latest docs /websites/postgresql_current "full text search to_tsvector plainto_tsquery websearch_to_tsquery ts_rank query matching ranking"
npx.cmd ctx7@latest library sqlalchemy "SQLAlchemy Core func PostgreSQL full text search functions select order_by label"
```

核对结论：

- PostgreSQL 官方文档使用 `to_tsvector(...) @@ to_tsquery(...)` 表达全文匹配，`@@` 返回文档向量是否匹配查询。
- `to_tsvector` 用于将文档字符串解析、规范化为 `tsvector`；`to_tsquery`、`plainto_tsquery`、`phraseto_tsquery` 等用于将用户文本转换为 `tsquery`。
- `ts_rank(...)` 可计算 `tsvector` 与 `tsquery` 的匹配排序分数，适合全文检索通道内部排序。
- PostgreSQL 官方文档建议对可搜索 `tsvector` 列建 GIN 索引以提升全文检索性能。当前模型已有 `DocChunk.content_tsv` 字段，且数据库 schema 已通过触发器在 `content` 写入或更新时自动维护该字段；Python 索引入库阶段只写 `content`，不显式写入 `content_tsv`，查询阶段也不使用表达式兜底。
- SQLAlchemy 2.0 官方文档入口为 `/websites/sqlalchemy_en_20`；实现阶段使用 SQLAlchemy Core `select(...)`、`func.*`、`label(...)`、`order_by(...)`、`where(...)` 表达 PostgreSQL 全文函数，具体表达式必须通过当前依赖测试验证。

## 2.1 任务背景

第 12 章基础 RAG 查询管道已经可以完成：

```plain
用户问题
  -> 读权限硬校验
  -> EmbeddingService.embed_query(...)
  -> ChunkRepository.search_by_vector(...)
  -> SourceBuilder.build(...)
  -> ChatOpenAI.ainvoke(...)
  -> ApiResponse[RagQueryResponse]
```

单路向量检索适合语义相近的问题，但对精确词、英文缩写、条款号、配置项、编号和专有名词不稳定。例如用户问 `Commit Message格式要求是什么？`，全文检索往往比向量检索更容易命中包含 `Commit Message` 的原文片段；用户问“新来的同事怎么配置开发电脑”时，向量检索又比字面匹配更能兜住语义改写。

本阶段目标是在不改变 API 入口和生成链路的前提下，引入全文检索通道，并使用 RRF（Reciprocal Rank Fusion）融合向量与全文两路排序结果：

```plain
RRF_score(chunk) = Σ 1 / (rrf_k + rank_i)
```

当前项目可复用的技术前提：

```plain
app/services/rag_query.py
  -> RagQueryService 已负责编排查询、拒答、上下文构建和模型生成

app/repositories/chunks.py
  -> ChunkRepository.search_by_vector(...) 已支持 PGVector 当前版本向量检索
  -> ChunkSearchHit 已包含来源元数据和 score

app/models/kb.py
  -> DocChunk 已有 content、content_tsv、section_title、embedding、doc_version
  -> KbDocument 已有 file_name、version、status、is_deleted

app/schemas/rag.py
  -> RagQueryRequest、SourceCitation、RagQueryResponse 已存在

app/core/config.py
  -> 已有 rag_vector_top_k、rag_fulltext_top_k、rag_return_top_n、rag_min_score；rag_min_score 当前不参与候选过滤
```

核心目标：

- 增加 PostgreSQL 全文检索召回，并与向量召回使用完全一致的权限、版本、状态和删除过滤。
- 使用 RRF 按排名融合两路候选，避免直接混加不可比的向量 score 与全文 rank。
- 保留 `RagQueryService` 作为基础向量 RAG 查询管道，新增 `RagQueryServiceV2` 调用混合检索服务，生成链路尽量不变。
- 明确混合检索后 `score`、`hit_count`、`sources` 的语义，避免把 RRF 分数误解为相似度百分比。
- 为第 15 章 Reranker 预留清晰输入：RRF 输出的有序 chunk 候选列表。

## 2.2 范围对齐

### In Scope

本阶段技术交付物：

- 新增 `app/services/ts_query_builder.py`，负责将用户问题转换为全文检索输入。
- 在 `app/services/hybrid_retriever.py` 内部实现 RRF 融合算法和结果数据结构，不单独拆出 `rrf.py`。
- 新增 `app/services/hybrid_retriever.py`，编排向量召回、全文召回、RRF 融合和候选数量统计。
- 扩展 `app/repositories/chunks.py`，增加 `search_by_fulltext(...)`，必要时调整 `ChunkSearchHit` 注释或新增混合命中 DTO。
- 新增 `app/services/rag_query_v2.py`，依赖 `HybridRetriever` 的最终候选结果；`app/services/rag_query.py` 保持基础向量 RAG 管道。
- 修改 `app/api/routes/rag.py` 的依赖组装函数，按 `rag_query_pipeline` 选择 `RagQueryService` 或 `RagQueryServiceV2`；`query_rag(...)` 路由函数、URL、请求体和响应体保持不变。
- 修改 `app/schemas/rag.py`，只同步 `hit_count` 和 `score` 语义，不新增响应字段。
- 修改 `app/core/config.py`，补充 `rag_rrf_k`；沿用或确认 `rag_fulltext_top_k`。
- 增加单元测试和 Repository 测试，覆盖 tsquery 构建、RRF 算法、全文 SQL 过滤、混合检索降级、RagQueryServiceV2 接入、基础管道保留和 API 权限短路。

### Out of Scope

本阶段不做：

- 不实现 Reranker 精排、Reranker 超时或 RRF 降级。
- 不实现 HyDE、多路查询、查询改写或 query expansion。
- 不实现全文检索专用外部引擎，如 Elasticsearch、OpenSearch、Meilisearch。
- 不强制引入 jieba、pg_jieba 或其他中文分词插件。
- 不实现精确 token 预算裁剪；仍复用第 12 章 SourceBuilder 的字符预算。
- 不实现 SSE 流式输出或多轮对话记忆。
- 不新增前端页面。
- 不改变上传、解析、分块、Embedding 入库和文档重建主流程；`content_tsv` 由数据库触发器根据 `content` 自动维护，Python 侧不显式赋值。
- 不把权限过滤交给 Prompt、前端或模型判断。

## 2.3 预计文件变更清单

### 新增文件

- `app/services/ts_query_builder.py`：构建 PostgreSQL 全文检索输入，处理停用词、标点拆分和空查询降级。
- `app/services/hybrid_retriever.py`：内聚 RRF 融合排序，按 `chunk_id` 去重并累加多路排名分。
- `app/services/hybrid_retriever.py`：混合检索服务，串联向量检索、全文检索、RRF 和统计字段。
- `tests/services/test_ts_query_builder.py`：覆盖全文查询词构建。
- `tests/services/test_hybrid_retriever.py`：覆盖 RRF 排名融合和混合检索编排。
- `tests/services/test_hybrid_retriever.py`：覆盖混合检索服务编排和降级。

### 修改文件

- `app/repositories/chunks.py`：新增 `search_by_fulltext(...)`，并统一返回与向量命中兼容的 `ChunkSearchHit`。
- `app/services/rag_query.py`：保持基础向量 RAG 查询管道。
- `app/services/rag_query_v2.py`：调用 `HybridRetriever.retrieve(...)`，并用 `hit_count` 返回实际进入 Prompt 的引用 chunk 数量。
- `app/api/routes/rag.py`：仅调整 `get_rag_query_service(...)` 依赖组装，按 `rag_query_pipeline` 选择 v1/v2；`query_rag(...)` 路由函数保持不变。
- `app/schemas/rag.py`：不新增响应字段，仅将 `SourceCitation.score` 说明调整为最终检索排序分。
- `app/core/config.py`：新增 `rag_rrf_k: int = 60`；确认 `rag_fulltext_top_k` 默认值。
- `tests/repositories/test_chunks.py`：新增全文检索 SQL 过滤断言。
- `tests/services/test_rag_query.py`：更新 Fake 依赖，覆盖混合检索结果、拒答和统计字段。
- `tests/api/test_rag.py`：保持权限失败短路，确保未触发混合检索服务。
- `docs/process/13-混合检索——向量检索与全文检索 RRF 融合-process.md`：实现后新增 Process 文档。

### 删除文件

不涉及。

## 2.4 输入/输出模型

### 2.4.1 查询接口

接口保持不变：

```plain
POST /api/v1/rag/query
Content-Type: application/json
```

请求体保持兼容：

```json
{
  "question": "Commit Message格式要求是什么？",
  "kb_ids": [2],
  "session_id": null
}
```

字段规则沿用第 12 章：

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `question` | string | 是 | 去除首尾空白后长度 1-2000 |
| `kb_ids` | list[int] | 是 | 至少 1 个，最多 20 个；去重后逐个校验读权限 |
| `session_id` | string \| null | 否 | 本阶段仍不读取或写入多轮会话 |

### 2.4.2 成功响应

响应结构保持第 12 章兼容，不新增原始召回数量字段：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "answer": "Commit Message 需要遵循约定格式，例如 type(scope): subject。[参考1]",
    "sources": [
      {
        "document_id": 1,
        "document_name": "研发规范.md",
        "kb_id": 2,
        "chunk_id": 10,
        "chunk_index": 3,
        "page_number": null,
        "section_title": "Commit Message",
        "score": 0.03226
      }
    ],
    "hit_count": 1,
    "latency_ms": 860
  }
}
```

字段语义：

| 字段 | 语义 |
| --- | --- |
| `hit_count` | 实际进入 Prompt 的引用 chunk 数量，与 `sources.length` 一致 |
| `sources` | 实际进入 Prompt 并返回给前端的引用来源，数量受 `rag_return_top_n` 和上下文预算影响 |
| `sources[].score` | 最终排序分；混合检索阶段为 RRF 分数，不是百分比相似度 |

建议 DTO：

```python
class RagQueryResponse(BaseModel):
    """RAG 查询响应体。"""

    answer: str
    sources: list[SourceCitation]
    hit_count: int
    latency_ms: int
```

`SourceCitation.score` 继续保留，混合检索后填入 `rrf_score`。如果后续需要调试向量分数和全文分数，不在本阶段 API 暴露；可以通过日志或内部 DTO 保留。

### 2.4.3 拒答响应

无有效候选时返回：

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

如果两路检索有原始召回但上下文预算导致没有可进入 Prompt 的候选，API 返回 `hit_count=0`、`sources=[]`；原始召回数量只写入日志，不进入响应模型。

### 2.4.4 内部数据结构

新增内部结果对象建议：

```python
@dataclass(frozen=True)
class RrfSearchHit:
    """RRF 融合后的检索命中。"""

    hit: ChunkSearchHit
    score: float
    retrieval_sources: tuple[str, ...]
```

如果为了复用 `SourceBuilder`，实现可以把 `RrfSearchHit` 转换回 `ChunkSearchHit`，其中 `score` 替换为 RRF 分数。该转换必须在 `HybridRetriever` 内完成，避免 `SourceBuilder` 关心检索通道细节。

```python
@dataclass(frozen=True)
class HybridRetrieveResult:
    """混合检索结果和统计信息。"""

    hits: list[ChunkSearchHit]
    vector_count: int
    fulltext_count: int
```

## 2.5 代码执行流程

### 2.5.1 总体调用链路

```plain
rag.query route
  -> RagQueryRequest 校验
  -> PermissionService.require_read(kb_id, user)
  -> RagQueryServiceV2.query(question, kb_ids, user)
  -> HybridRetriever.retrieve(question, kb_ids)
     -> EmbeddingService.embed_query(question)
     -> ChunkRepository.search_by_vector(query_vector, kb_ids, rag_vector_top_k)
     -> TsQueryBuilder.build(question)
     -> ChunkRepository.search_by_fulltext(ts_query_text, kb_ids, rag_fulltext_top_k)
     -> _rrf_fuse({"vector": vector_hits, "fulltext": fulltext_hits}, rrf_k)
     -> HybridRetrieveResult(hits, vector_count, fulltext_count)
  -> 无有效候选时固定拒答
  -> SourceBuilder.build(hits, return_top_n=rag_return_top_n)
  -> ChatOpenAI.ainvoke([SystemMessage, HumanMessage])
  -> RagQueryResponse(answer, sources, hit_count, latency_ms)
```

### 2.5.2 依赖组装

`app/api/routes/rag.py` 中的 `get_rag_query_service(...)` 调整为：

```plain
get_rag_query_service(session, settings)
  -> chunk_repository = ChunkRepository(session)
  -> embedding_service = EmbeddingService(...)
  -> ts_query_builder = TsQueryBuilder()
  -> hybrid_retriever = HybridRetriever(
       embedding_service=embedding_service,
       chunk_repository=chunk_repository,
       ts_query_builder=ts_query_builder,
       settings=settings,
     )
  -> RagQueryServiceV2(
       retriever=hybrid_retriever,
       source_builder=SourceBuilder(...),
       chat_model=get_chat_model(),
       settings=settings,
     )
```

`RagQueryServiceV2` 不直接持有 `EmbeddingService` 和 `ChunkRepository`。基础 `RagQueryService` 仍保留向量检索管道，路由依赖组装通过 `rag_query_pipeline` 选择 v1 或 v2。

### 2.5.3 路由层无感升级

本阶段必须像参考文献中“Controller 只替换注入实现”一样，做到 API 调用方无感升级。当前 FastAPI 版本不需要修改 `query_rag(...)` 路由函数，只允许修改 `get_rag_query_service(...)` 内部依赖组装。

保持不变：

```python
@router.post("/query")
async def query_rag(
    request: RagQueryRequest,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    rag_service: RagQueryPipeline = Depends(get_rag_query_service),
) -> ApiResponse[RagQueryResponse]:
    for kb_id in request.kb_ids:
        await permission_service.require_read(kb_id, user)

    return ApiResponse.ok(
        await rag_service.query(
            question=request.question,
            kb_ids=request.kb_ids,
            user=user,
        )
    )
```

允许变化：

- `get_rag_query_service(...)` 内部根据 `rag_query_pipeline` 选择基础 `RagQueryService` 或混合检索 `RagQueryServiceV2`。
- `RagQueryService` 保持纯向量检索实现，`RagQueryServiceV2` 使用混合检索实现。
- `SourceCitation.score` 的语义从向量相似度分数调整为 RRF 排序分。

禁止变化：

- 不修改 `POST /api/v1/rag/query` 路径。
- 不修改 `RagQueryRequest` 请求体字段。
- 不新增响应字段。
- 不把权限校验下沉到 Prompt 或检索服务之后。
- 不要求前端传入“是否启用混合检索”的开关。

### 2.5.4 TsQueryBuilder

新增文件：

```plain
app/services/ts_query_builder.py
```

职责：

- 清理用户问题首尾空白。
- 按空白和常见中英文标点拆分 token。
- 过滤停用词和无意义短 token。
- 保留英文缩写、数字编号、下划线、连字符、点号条款等精确词。
- 返回可传给 PostgreSQL `to_tsquery('simple', ...)` 的 `关键词 & 关键词` 字符串；无有效 token 时返回 `None`。

建议方法：

```python
class TsQueryBuilder:
    """构建 PostgreSQL 全文检索查询文本。"""

    def build(self, question: str) -> str | None:
        """将用户问题转换为全文检索输入。"""
```

本阶段按参考文献使用 `to_tsquery('simple', query_text)`，其中 `query_text` 由 `TsQueryBuilder` 生成，格式为 `关键词 & 关键词`。这要求查询词构建阶段过滤停用词和明显无意义 token，避免把空白或无效输入传给 PostgreSQL。

停用词建议内置在模块中：

```python
STOP_WORDS = {
    "的", "了", "是", "在", "有", "和", "与", "或", "这", "那",
    "什么", "怎么", "如何", "为什么", "哪些", "怎样", "请问",
    "a", "an", "the", "is", "are", "what", "how",
}
```

### 2.5.5 全文检索 Repository

修改文件：

```plain
app/repositories/chunks.py
```

新增方法：

```python
async def search_by_fulltext(
    self,
    *,
    query_text: str,
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
DocChunk.content_tsv @@ to_tsquery('simple', :query_text)
ORDER BY ts_rank(DocChunk.content_tsv, to_tsquery('simple', :query_text)) DESC
LIMIT top_k
```

全文检索严格使用 `DocChunk.content_tsv`。该字段由数据库触发器维护，Python 写入 chunk 时只保存 `content`：

```plain
NEW.content_tsv := to_tsvector('simple', NEW.content)
```

全文检索返回的 `ChunkSearchHit.score` 使用全文 `ts_rank`，但该分数只用于全文通道内部排序和调试；RRF 后对外返回的 `score` 应改为 RRF 分数。

### 2.5.6 RRF 融合模块

新增文件：

```plain
app/services/hybrid_retriever.py
```

建议函数：

```python
def _rrf_fuse(
    ranked_results: dict[str, list[ChunkSearchHit]],
    *,
    rrf_k: int,
) -> list[RrfSearchHit]:
    """按 RRF 分数融合多路已排序检索结果。"""
```

融合规则：

```plain
for source_name, hits in ranked_results.items():
    for rank, hit in enumerate(hits, start=1):
        score = 1 / (rrf_k + rank)
        chunk_id 相同则累加 score
        retrieval_sources 累加 source_name

最终按 score 降序；同分时按首次出现顺序稳定排序
```

默认 `rrf_k=60`，来自配置 `settings.rag_rrf_k`。`rrf_k <= 0` 应视为配置错误，Spec 建议在配置校验或服务初始化时拒绝。

### 2.5.7 HybridRetriever

新增文件：

```plain
app/services/hybrid_retriever.py
```

本章定义的是初始混合检索能力；当前实现已由检索层权限过滤 Spec 扩展为受保护的 `retrieve(...)` 入口，并通过构造参数注入
`PermissionService`。原始 `_retrieve(...)` 只接受已过滤的 `allowed_kb_ids`，具体错误语义、审计日志和 HyDE 范围传播以该 Spec
及 ADR-0007 为准。

建议构造参数：

```python
class HybridRetriever:
    """混合检索服务，融合向量检索和全文检索结果。"""

    def __init__(
        self,
        *,
        embedding_service: EmbeddingService,
        chunk_repository: ChunkRepository,
        ts_query_builder: TsQueryBuilder,
        settings: Settings,
    ) -> None: ...
```

建议核心方法：

```python
async def retrieve(
    self,
    *,
    question: str,
    kb_ids: list[int],
) -> HybridRetrieveResult:
    """执行向量召回、全文召回和 RRF 融合。"""
```

执行流程：

```plain
1. query_vector = await embedding_service.embed_query(question)
2. vector_hits = await chunk_repository.search_by_vector(...)
3. query_text = ts_query_builder.build(question)
4. if query_text:
       fulltext_hits = await chunk_repository.search_by_fulltext(...)
   else:
       fulltext_hits = []
5. fused_hits = _rrf_fuse({"vector": vector_hits, "fulltext": fulltext_hits}, rrf_k=settings.rag_rrf_k)
6. return HybridRetrieveResult(
       hits=[to_chunk_search_hit(hit) for hit in fused_hits],
       vector_count=len(vector_hits),
       fulltext_count=len(fulltext_hits),
   )
```

当前阶段向量通道和全文通道都不应用 `rag_min_score`。全文 `ts_rank`、向量相似度和 RRF 分数尺度不同，固定阈值过滤留到后续阶段重新规划；本阶段只按 `rag_return_top_n` 和上下文预算裁剪最终进入 Prompt 与 `sources` 的 chunk。

### 2.5.8 RagQueryServiceV2 调整

新增 `app/services/rag_query_v2.py`，保留 `app/services/rag_query.py` 作为基础向量 RAG 查询管道。`RagQueryServiceV2` 构造参数如下：

```python
class RagQueryServiceV2:
    def __init__(
        self,
        *,
        retriever: HybridRetriever,
        source_builder: SourceBuilder,
        chat_model: Any,
        settings: Settings,
    ) -> None: ...
```

主流程调整：

```plain
1. started_at = perf_counter()
2. normalized_question = question.strip()
3. retrieve_result = await retriever.retrieve(question=normalized_question, kb_ids=kb_ids)
4. hits = retrieve_result.hits
5. if not hits: return refusal_response(started_at)
6. context, sources = source_builder.build(hits, return_top_n=settings.rag_return_top_n)
7. answer = await generate_answer(...)
8. return RagQueryResponse(
       answer=answer,
       sources=sources,
       hit_count=len(sources),
       latency_ms=...
   )
```

Embedding 失败、SQLAlchemy 检索失败、Chat 模型失败的错误语义沿用第 12 章。全文查询词为空不是错误。

## 2.6 技术约束与最佳实践

- 两路检索必须使用相同权限和版本过滤，避免全文通道绕过 `kb_id`、`doc_version`、`DONE` 或 `is_deleted` 约束。
- RRF 只使用排名融合，不直接混加向量 `score` 和全文 `ts_rank`，因为两者尺度不同。
- 当前阶段不使用 `rag_min_score` 过滤候选；RRF 输出分数不能解释为相似度百分比。相似度阈值过滤后续单独设计。
- 全文查询词构建失败或为空时必须降级为空全文结果，不能让用户输入导致 500。
- 本阶段严格复用 `DocChunk.content_tsv`；该字段由数据库触发器写入 `to_tsvector('simple', NEW.content)`，Python 索引入库不显式赋值，查询阶段不使用表达式 `to_tsvector(...)` 降级。

## 2.7 验收标准

### 场景一：混合检索正常召回

GIVEN 用户对有读权限知识库提问  
WHEN 调用 `POST /api/v1/rag/query`  
THEN 服务执行向量召回和全文召回，使用 RRF 融合结果进入 `SourceBuilder`，响应包含 `hit_count`、`sources` 和 `latency_ms`。

### 场景二：精确关键词全文补强

GIVEN 文档 chunk 中包含 `Commit Message`  
WHEN 用户提问 `Commit Message格式要求是什么？`  
THEN 全文检索通道能召回包含该关键词的 chunk，并参与 RRF 排序。

### 场景三：语义问题向量兜底

GIVEN 用户问题与原文没有明显字面重合  
WHEN 全文检索没有命中  
THEN 向量检索结果仍参与 RRF，服务可基于向量候选生成答案。

### 场景四：重复 chunk 去重

GIVEN 同一个 `chunk_id` 同时出现在向量结果和全文结果中  
WHEN 执行 RRF 融合  
THEN 最终候选中该 `chunk_id` 只出现一次，RRF 分数为两路排名分累加。

### 场景五：全文查询词为空降级

GIVEN 用户问题只能提取出停用词或空白  
WHEN `TsQueryBuilder.build(...)` 返回 `None`  
THEN 全文通道返回空结果，服务继续使用向量通道结果，不抛出 500。

### 场景六：权限失败短路

GIVEN 请求中存在无读权限 `kb_id`  
WHEN 调用查询接口  
THEN 路由层返回权限错误，且不调用 `EmbeddingService`、`HybridRetriever`、`ChunkRepository` 或聊天模型。

### 场景七：全文 SQL 权限和版本过滤

GIVEN Repository 执行全文检索  
WHEN 构造 SQLAlchemy statement  
THEN 查询条件包含 `DocChunk.kb_id.in_(kb_ids)`、`DocChunk.doc_version == KbDocument.version`、`KbDocument.status == DONE`、`KbDocument.is_deleted.is_(False)`。

### 场景八：无有效候选拒答

GIVEN 向量通道和全文通道均无有效候选  
WHEN 执行查询  
THEN 响应返回固定拒答文案，`sources=[]`、`hit_count=0`，且不调用聊天模型。

### 场景九：字段语义清晰

GIVEN 向量召回 18 条、全文召回 12 条、RRF 去重后 7 条候选  
WHEN 返回查询响应  
THEN 响应中 `hit_count` 等于 `sources` 数量，且不超过 `rag_return_top_n` 和上下文预算；原始召回数量只进入日志或内部统计。

### 场景十：验证命令

实现完成后至少运行：

```powershell
uv run pytest tests/services/test_ts_query_builder.py -v
uv run pytest tests/services/test_hybrid_retriever.py -v
uv run pytest tests/services/test_hybrid_retriever.py -v
uv run pytest tests/repositories/test_chunks.py -v
uv run pytest tests/services/test_rag_query.py -v
uv run pytest tests/api/test_rag.py -v
uv run ruff check app tests
```

若仅新增或修改文档，则至少重新读取目标 Spec 文件，检查文件名后缀、必填章节、占位符和链接。

### 文档同步

- 本文件名必须保持 `*-spec.md` 后缀：`13-混合检索——向量检索与全文检索 RRF 融合-spec.md`。
- 对应 Plan 已存在：`docs/plans/13-混合检索——向量检索与全文检索 RRF 融合-plan.md`。
- 代码实现完成后，需要新增对应 Process 文档：`docs/process/13-混合检索——向量检索与全文检索 RRF 融合-process.md`。
- 如果实现阶段决定新增数据库迁移、引入中文分词依赖、改变响应字段或增加全文-only 降级，必须先同步更新本 Spec。
