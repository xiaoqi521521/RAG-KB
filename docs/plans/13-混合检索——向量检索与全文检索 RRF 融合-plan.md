# 13-混合检索——向量检索与全文检索 RRF 融合 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to implement this plan task-by-task. Steps in后续 Spec / Process 可继续使用 checkbox 跟踪。

本文基于 [13-混合检索——向量检索与全文检索 RRF 融合.md](/D:/Code/python/Practical_Project/rag-kb/docs/references/13-混合检索——向量检索与全文检索%20RRF%20融合.md)、[三阶段生成.md](/D:/Code/python/Practical_Project/rag-kb/docs/prompt/三阶段生成.md) 的 Plan 阶段规则，以及当前已落地的 [12-基础 RAG 查询管道——检索与生成-plan.md](/D:/Code/python/Practical_Project/rag-kb/docs/plans/12-基础%20RAG%20查询管道——检索与生成-plan.md)，梳理 LangChain / FastAPI 版本的混合检索升级计划。

本阶段目标是在现有基础 RAG 查询管道上，把“单路向量检索”升级为“向量检索 + PostgreSQL 全文检索 + RRF 融合排序”。接口入口和生成链路保持稳定，主要替换检索层，让系统同时覆盖语义改写问题和精确关键词问题。

## 1.1 任务背景

第 12 章基础 RAG 查询管道已经完成如下闭环：

```plain
用户问题
  -> 权限校验
  -> EmbeddingService.embed_query(...)
  -> ChunkRepository.search_by_vector(...)
  -> SourceBuilder.build(...)
  -> ChatOpenAI.ainvoke(...)
  -> 返回 answer / sources / hit_count / latency_ms
```

这个链路能覆盖语义相近的问题，但单纯向量检索存在明显盲区：精确词、编号、条款、英文缩写、SKU、PO 号、接口名、配置项、制度条款号等内容，往往需要字面匹配。参考文献第 13 章给出的方向是增加全文检索，再用 RRF（Reciprocal Rank Fusion）把两路结果融合为统一排序。

当前项目中可复用的基础包括：

- `RagQueryService`：已经负责查询主流程、拒答、Prompt 组装和模型生成。
- `ChunkRepository.search_by_vector(...)`：已经能在权限范围内按 PGVector 距离召回当前版本 chunk。
- `ChunkSearchHit`：已经包含文档名、知识库 ID、chunk 元数据、内容和向量 score，可作为混合检索命中结构的基础。
- `SourceBuilder`：已经能把检索命中转换为 `[参考N]` 上下文和结构化 sources。
- `Settings`：已有 `rag_vector_top_k`、`rag_fulltext_top_k`、`rag_return_top_n`、`rag_rrf_k`、`rag_query_pipeline` 等 RAG 参数。
- `PermissionService.require_read(...)`：已经在路由层完成检索前硬权限校验。

本阶段要解决的核心问题：

- 向量检索适合语义相近问题，但对精确关键词不稳定。
- 全文检索适合字面匹配，但对语义改写、近义表达不稳定。
- 两路结果的分数尺度不同，不能直接按原始分数相加。
- RRF 使用排名而不是原始分数融合，适合把不同检索通道的 TopK 结果合并。

迁移到当前 Python 项目时，需要把参考文献中的 Spring `HybridRetrieverService` 落到清晰的服务层模块：全文查询词构建、向量召回、全文召回、RRF 融合、最终命中过滤。RRF 只由 `HybridRetriever` 使用，因此作为其内部实现保留。生成阶段新增 `RagQueryServiceV2` 使用混合检索结果，原 `RagQueryService` 保持基础向量 RAG 管道。

## 1.2 业务流程

### 1.2.1 总体执行链路

混合检索查询流程：

```plain
1. 接收查询请求：沿用 `POST /api/v1/rag/query`，输入仍为 `question`、`kb_ids`、可选 `session_id`
2. 检索前权限校验：路由层继续逐个调用 `PermissionService.require_read(kb_id, user)`
3. 查询向量化：调用 `EmbeddingService.embed_query(question)` 生成查询向量
4. 向量召回：在允许知识库范围内按 PGVector 距离召回 `rag_vector_top_k` 个候选
5. 全文召回：将 question 转换为 PostgreSQL tsquery，在允许知识库范围内召回 `rag_fulltext_top_k` 个候选
6. RRF 融合：按每路排名计算 `1 / (rrf_k + rank)`，同一 chunk 多路命中时累加
7. 候选裁剪：按融合排序取候选，最终由 `RAG_RETURN_TOP_N` 和上下文预算决定进入 Prompt 的 chunk
8. 生成回答：复用 `SourceBuilder` 和 `ChatOpenAI`，返回 answer、sources、hit_count、latency_ms
```

### 1.2.2 完整业务流程

#### 查询入口保持不变

本阶段不新增 HTTP 接口。调用方仍使用：

```plain
POST /api/v1/rag/query
```

请求体保持兼容：

```json
{
  "question": "Commit Message格式要求是什么？",
  "kb_ids": [2],
  "session_id": null
}
```

入口层职责不变：

- 校验 `question` 非空。
- 校验 `kb_ids` 非空、去重且为正整数。
- 对每个 `kb_id` 执行读权限硬校验。
- 权限失败时不执行向量化、全文检索或模型调用。

#### 查询词处理

全文检索需要把自然语言问题转换为 PostgreSQL 可执行的查询表达式。参考文献中的 `TsQueryBuilder` 采用简单分词、过滤停用词、用 `&` 连接关键词。Python 版计划先实现轻量查询词构建器，不在本阶段引入复杂中文分词依赖。

建议流程：

```plain
原始问题
  -> strip
  -> 按空白、常见中英文标点拆分
  -> 过滤停用词和过短 token
  -> 保留英文缩写、数字编号、条款号、连字符词等精确关键词
  -> 生成 `关键词 & 关键词` 形式的 to_tsquery 输入
```

如果拆分后没有有效关键词，全文检索通道应安全降级为空结果，不能影响向量检索通道。中文全文检索效果取决于 PostgreSQL 配置和文本预处理能力，本阶段计划先使用数据库原生能力和简单 token 处理，后续再评估 jieba、pg_jieba 或专用检索引擎。

#### 向量召回

向量召回继续复用当前基础能力：

```plain
EmbeddingService.embed_query(question)
  -> ChunkRepository.search_by_vector(query_vector, kb_ids, rag_vector_top_k)
```

向量通道必须保留第 12 章已经建立的硬过滤：

```plain
DocChunk.kb_id IN allowed_kb_ids
DocChunk.doc_version = KbDocument.version
KbDocument.status = DONE
KbDocument.is_deleted = false
ORDER BY DocChunk.embedding <=> query_vector
LIMIT rag_vector_top_k
```

向量通道的职责是覆盖语义改写问题，例如用户问“新来的同事怎么配置开发电脑”，文档中写“入职第一天领取笔记本电脑并安装开发环境”。

#### 全文召回

全文召回新增 Repository 方法，查询范围必须与向量通道一致。当前实现使用 `content_tsv @@ to_tsquery('simple', query_text)`，其中 `content_tsv` 由数据库触发器维护，Python 索引入库不显式写入该字段：

```plain
DocChunk.kb_id IN allowed_kb_ids
DocChunk.doc_version = KbDocument.version
KbDocument.status = DONE
KbDocument.is_deleted = false
content_tsv @@ to_tsquery('simple', query_text)
LIMIT rag_fulltext_top_k
```

全文通道的职责是覆盖精确词问题，例如：

- `Commit Message`
- `PO 号`
- `SKU-8821`
- `第3.2条`
- `API_KEY`
- `RRF`

全文结果应返回与向量命中兼容的结构，至少包含 `chunk_id`、`document_id`、`document_name`、`kb_id`、`chunk_index`、`content`、`page_number`、`section_title` 和全文 rank / score。由于 RRF 只依赖排名，全文原始 score 不需要直接参与最终融合分数，但可以保留用于日志或调试。

#### RRF 融合

RRF 按排名融合多路结果，不要求不同通道原始分数可比。参考文献公式：

```plain
RRF_score(doc) = Σ 1 / (k + rank_i)
k = 60
```

Python 版将 RRF 保留在 `hybrid_retriever.py` 内部，输入为有序命中列表：

```plain
vector_hits: [chunk101, chunk102, chunk103]
fulltext_hits: [chunk102, chunk105, chunk101]

chunk101 = 1/(60+1) + 1/(60+3)
chunk102 = 1/(60+2) + 1/(60+1)
chunk103 = 1/(60+3)
chunk105 = 1/(60+2)
```

融合要求：

- 同一 `chunk_id` 在多路结果中出现时只保留一条命中，RRF 分数累加。
- 排名从 1 开始计算。
- 最终按 `rrf_score` 降序排序。
- 分数相同时保持稳定排序，优先保留更早出现在向量或全文结果中的命中。
- 每条融合命中保留来源标记，例如 `retrieval_sources=["vector", "fulltext"]`，便于日志和后续评估。

#### 候选数量和阈值语义

当前实现保留 `rag_min_score` 配置，但 v1 和 v2 查询管道都暂不使用它过滤候选。混合检索后最终 `score` 会变成 RRF 分数，不能直接沿用向量相似度阈值语义，否则会混淆：

```plain
vector score = 1 / (1 + distance)
rrf score = Σ 1 / (rrf_k + rank)
```

本阶段在 Spec / Process 中明确：

- `rag_min_score` 当前不参与向量通道、全文通道或 RRF 输出过滤。
- 复用 `rag_return_top_n` 控制最多进入上下文和响应 sources 的 chunk 数量。
- RRF 分数默认用于排序，不直接作为“语义相似度百分比”解释。
- 是否引入向量原始分阈值或 RRF 阈值需要谨慎，后续应单独设计。

为了保持接口体验，本阶段建议：

```plain
hit_count = 实际进入 Prompt 的引用 chunk 数量，与 sources 数量一致
sources = 实际进入 Prompt 的引用 chunk
```

本阶段不新增 `retrieved_count` 响应字段。向量召回数、全文召回数和 RRF 融合候选数只进入日志或内部统计，避免把 API 响应变成调试结构。

#### 与生成链路衔接

混合检索服务返回统一命中列表后，现有生成链路尽量不变：

```plain
HybridRetriever.retrieve(question, kb_ids)
  -> list[HybridSearchHit]
  -> RagQueryServiceV2 无召回判断
  -> SourceBuilder.build(...)
  -> ChatOpenAI.ainvoke(...)
```

`SourceBuilder` 不应该关心命中来自向量还是全文，只需要按最终排序生成 `[参考N]`。如果要在 `sources` 中展示通道信息，可以作为后续调试字段或评估字段，不建议第一版直接暴露给前端。

#### 低召回和降级

本阶段有两个降级边界：

- 全文查询词为空或全文 SQL 无结果：保留向量结果继续 RRF，不能拒绝整个查询。
- 向量 provider 失败：当前基础 RAG 中向量化失败会返回 503；本阶段仍保持该行为，不新增“全文-only”降级，避免改变错误语义。后续如需全文-only 降级，应单独设计。

如果两路都没有有效候选，返回第 12 章固定拒答文案，不调用聊天模型。

#### 日志与可观测性

混合检索需要比基础向量检索多记录几类数据：

```plain
vector_count
fulltext_count
merged_count
returned_count
rrf_k
vector_elapsed_ms
fulltext_elapsed_ms
rrf_elapsed_ms
```

日志不能输出完整 Prompt 或大段 chunk 内容。可以输出 query 的长度、kb_ids、命中数量、最高 RRF 分数和各通道耗时。

### 1.2.3 关键分支与边界条件

#### 分支一：精确关键词由全文命中补强

用户问题包含 `Commit Message`、`SKU-8821`、`第3.2条` 等字面关键词时，全文检索能把精确匹配 chunk 排到较前位置。若该 chunk 同时被向量检索召回，RRF 分数累加，最终排序进一步提前。

#### 分支二：语义改写由向量兜底

用户问题与原文没有明显字面重合时，全文检索可能为空或弱命中，但向量检索仍能召回语义相近 chunk。RRF 对单通道命中仍给分，保证语义场景不会因为全文无结果而失败。

#### 分支三：两路召回重复

同一个 chunk 同时出现在向量和全文结果中时，只保留一条最终命中，RRF 分数累加。sources 中不能重复展示同一个 `chunk_id`。

#### 分支四：全文查询词无法构建

如果问题太短、只有停用词或无法安全构建 tsquery，全文通道返回空列表，检索继续使用向量结果。该场景应记录 debug 日志，不能抛出 500。

#### 分支五：权限和版本过滤

向量和全文两路 SQL 都必须包含相同的 `kb_id`、当前 `doc_version`、`DONE` 状态和未删除过滤。不能出现向量通道过滤严格、全文通道漏掉权限或旧版本的情况。

#### 分支六：无有效候选

如果向量和全文都无结果，或最终没有可进入上下文的候选，服务返回“在知识库中未找到相关内容”，并且不调用聊天模型。

## 1.3 业务边界

### In Scope

本阶段必须覆盖的业务范围：

- 在现有 `/api/v1/rag/query` 查询链路中引入混合检索，不新增前端页面和新 HTTP 路径。
- 新增全文检索查询词构建能力，支持中英文标点拆分、停用词过滤和空查询降级。
- 扩展 `ChunkRepository`，新增 PostgreSQL 全文检索方法，并复用权限、版本、状态和删除过滤。
- 保留现有 PGVector 向量召回，向量 TopK 和全文 TopK 分别配置。
- 在 `HybridRetriever` 内部实现 RRF 融合，按 `chunk_id` 去重并按 RRF 分数降序输出。
- 新增混合检索服务，负责串联向量召回、全文召回、RRF 融合和候选排序。
- 新增 `RagQueryServiceV2`，使用混合检索结果输入，生成链路和拒答策略保持稳定；原 `RagQueryService` 保留基础向量检索管道。
- 明确 `score`、`hit_count`、`sources` 等字段在混合检索后的语义，避免把 RRF 分数解释为相似度百分比。
- 增加单元测试覆盖 RRF 算法、全文查询词构建、全文 SQL 过滤、重复 chunk 去重和混合检索接入。
- 同步更新对应 Spec / Process 文档，记录最终字段语义、配置项和验证结果。

### Out of Scope

本阶段不实现或不展开：

- 不实现 Reranker 精排、Reranker 超时和 RRF 降级；这些属于第 15 章。
- 不实现 HyDE、多路查询和查询改写；这些属于第 14 章。
- 不实现精确 Token 预算裁剪；这些属于第 16 章。
- 不实现完整引用溯源、防幻觉二次校验和事实性评估；这些属于第 17 章。
- 不实现 SSE 流式输出、多轮对话记忆或会话标题生成；这些属于第 18 章。
- 不引入 Elasticsearch、OpenSearch、Meilisearch 等外部检索引擎。
- 不强制引入 jieba、pg_jieba 或其他中文分词插件；如 Spec 阶段决定引入，必须说明依赖、迁移和部署影响。
- 不修改文档解析、分块、Embedding 入库和重建索引流程。
- 不新增数据库表；如全文检索需要索引优化，优先评估现有字段和可选数据库索引变更，并在 Spec 阶段单独列出。
- 不把权限过滤交给 Prompt 或前端控制。

## 1.4 方案取舍

### 推荐方案：显式 HybridRetriever 服务 + 内部 RRF 融合

推荐新增 `HybridRetriever` 服务，内部显式调用向量 Repository 和全文 Repository，并在同一文件内通过 `_rrf_fuse(...)` 输出统一命中列表。`RagQueryServiceV2` 只关心最终命中，不关心每路召回细节。

优点：

- 检索增强集中在检索层，生成层改动小。
- RRF 算法可在 `HybridRetriever` 测试中独立覆盖。
- 向量、全文、融合三段耗时和命中数量可分别观测。
- 后续第 15 章接入 Reranker 时，可以直接把 RRF 输出作为 rerank 输入。

代价：

- 需要新增几个小模块和测试。
- 需要重新定义混合检索后的 `score` 和 `hit_count` 语义。

### 备选方案一：直接在 RagQueryService 中写两路检索

把向量召回、全文召回和 RRF 都写进 `RagQueryService.query(...)`。

优点是代码路径短。缺点是查询服务会变得臃肿，后续 Reranker、查询改写和评估指标接入时很难维护，不符合项目“核心业务逻辑保持小模块”的约束。

### 备选方案二：只加全文检索，不做 RRF

先全文召回，再向量召回，简单拼接去重。

优点是实现更快。缺点是排序没有统一依据，容易让某一路结果固定压过另一路，无法体现参考文献第 13 章的核心价值。

本 Plan 推荐采用“显式 HybridRetriever 服务 + 内部 RRF 融合”。

## 1.5 与当前代码的衔接计划

后续 Spec 可围绕以下模块拆分：

```plain
app/services/ts_query_builder.py
  -> 新增全文查询词构建器，负责停用词过滤和空查询降级

app/services/hybrid_retriever.py
  -> 新增混合检索服务，编排向量召回、全文召回和内部 RRF 融合

app/repositories/chunks.py
  -> 新增 search_by_fulltext(...)
  -> 视需要调整 ChunkSearchHit 或新增 HybridSearchHit

app/services/rag_query.py
  -> 保留基础向量 RAG 查询管道

app/services/rag_query_v2.py
  -> 调用 HybridRetriever，承接混合检索 RAG 查询管道
  -> 保持拒答、SourceBuilder 和 ChatOpenAI 生成链路稳定

app/core/config.py
  -> 增加 rag_fulltext_top_k、rag_rrf_k、rag_query_pipeline 等配置

app/schemas/rag.py
  -> 不新增 retrieved_count，仅同步 score 和 hit_count 语义
```

建议新增或调整的测试：

```plain
tests/services/test_ts_query_builder.py
  -> 覆盖停用词过滤、英文关键词、编号/条款号、空查询降级

tests/repositories/test_chunks.py
  -> 覆盖全文检索 SQL 包含 kb_id、doc_version、DONE、未删除过滤

tests/services/test_hybrid_retriever.py
  -> 覆盖 RRF 排序、向量 + 全文召回数量、全文空结果降级

tests/services/test_rag_query.py
  -> 覆盖 RagQueryService 基础向量管道生成答案和拒答

tests/services/test_rag_query_v2.py
  -> 覆盖 RagQueryServiceV2 使用混合检索结果生成答案和拒答

tests/api/test_rag.py
  -> 保持权限失败短路，不触发混合检索
```

## 1.6 验收方向

本 Plan 不直接修改代码，但后续 Spec 和实现应能验证以下行为：

- 有读权限用户调用 `/api/v1/rag/query` 时，服务同时执行向量召回和全文召回。
- 任一 `kb_id` 无读权限时，请求在路由层失败，不触发 embedding、全文检索或生成。
- 向量检索 SQL 和全文检索 SQL 都包含知识库、当前版本、DONE 状态和未删除过滤。
- 全文查询词为空时，全文通道返回空结果，向量通道仍可正常生成答案。
- 同一个 chunk 同时被向量和全文召回时，RRF 分数累加，最终 sources 中只出现一次。
- RRF 排序使用 `1 / (rrf_k + rank)`，默认 `rrf_k=60`，排名从 1 开始。
- 精确关键词问题相比纯向量检索更容易召回包含关键词的 chunk。
- 语义改写问题在全文无强命中时仍能依赖向量召回。
- 无任何有效候选时返回固定拒答，不调用聊天模型。
- 正常响应中的 `hit_count` 与 `sources.length` 一致，不把 RRF 分数误称为相似度百分比。
- 日志能区分 `vector_count`、`fulltext_count`、`merged_count`、`returned_count` 和各阶段耗时。
- 文档文件名符合 Plan 阶段要求，使用 `-plan.md` 后缀。

## 1.7 下一步文档建议

完成本 Plan 后，下一份 Spec 应重点回答：

- 混合检索后的 `score` 字段是否返回 RRF 分数，是否需要新增 `vector_score` / `fulltext_score` 内部调试字段。
- 后续相似度阈值过滤是否重新启用，以及它应作用于向量原始分、全文 rank、RRF 分数还是 Reranker 分数。
- PostgreSQL 全文检索使用 `to_tsquery('simple', query_text)`，中文文本如何处理。
- 是否需要数据库索引支持全文检索，例如 `to_tsvector(...)` GIN 索引；若需要，是否新增迁移。
- `HybridRetriever`、内部 RRF、`TsQueryBuilder` 的具体文件路径、方法签名和返回 DTO。
- 无召回拒答、日志字段和 API 响应字段如何与第 12 章兼容。
