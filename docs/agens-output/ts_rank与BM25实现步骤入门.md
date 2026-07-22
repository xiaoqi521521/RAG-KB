# ts_rank 与 BM25 实现步骤入门

本文面向第一次接触 PostgreSQL 全文检索和 BM25 的读者，说明本项目当前使用的 `ts_rank` 链路，以及如果以后改用 BM25，需要增加哪些步骤。

## 先记住三个概念

1. **关键词检索**：根据文本中是否出现相同词语来找文档。
2. **向量检索**：把问题和文档转换成向量，根据语义相似度检索。
3. **排序算法**：检索命中后，决定哪些文档排在前面。

`ts_rank` 和 BM25 都属于关键词检索中的排序方法。它们都不是向量检索，也不是 RRF。

```text
关键词检索：先匹配文本，再排序
向量检索：先比较向量相似度，再排序
RRF：把已经排好序的多路结果再次合并
```

## 一、本项目当前的 ts_rank 链路

当前项目没有实现 BM25，使用的是 PostgreSQL 原生全文检索：

```text
文档正文
  -> to_tsvector('simple', content)
  -> content_tsv
  -> GIN 索引

用户问题
  -> 提取关键词
  -> to_tsquery('simple', ...)
  -> content_tsv @@ query
  -> ts_rank 排序
```

相关实现：

- `app/db/schema.sql`：创建 `content_tsv`、GIN 索引和触发器。
- `app/services/ts_query_builder.py`：把用户问题转换为关键词查询。
- `app/repositories/chunks.py`：执行 PostgreSQL 全文匹配和 `ts_rank` 排序。
- `app/services/hybrid_retriever.py`：把向量和全文两路结果交给 RRF。

### 第 1 步：保存原始 chunk

索引管道把文档切成 chunk，写入 `kb_doc_chunk.content`。每个 chunk 还带有 `kb_id`、文档版本、章节和页码等元数据。

当前默认 chunk 大小约为 512 个字符，重叠约为 64 个字符。

### 第 2 步：生成 `tsvector`

数据库触发器在 chunk 插入或正文更新时执行：

```sql
NEW.content_tsv := to_tsvector('simple', NEW.content);
```

可以把 `tsvector` 理解成 PostgreSQL 为全文检索准备的“词项索引表示”。它不是原始正文，而是经过解析后的词项信息。

本项目使用 `simple` 配置。它不提供专门的中文分词，所以中文检索效果有限，主要由向量检索补充。

### 第 3 步：创建 GIN 索引

```sql
CREATE INDEX idx_chunk_content_tsv
ON kb_doc_chunk USING GIN (content_tsv);
```

GIN 索引的作用是加速“哪些 chunk 包含这些词”的查找。

注意：GIN 索引只是索引结构，不等于 BM25。当前项目的排序函数仍然是 `ts_rank`。

### 第 4 步：清洗用户问题

`TsQueryBuilder` 会：

- 提取英文、数字、符号和中文片段；
- 去掉“什么、怎么、如何”等停用词；
- 去掉常见标点；
- 用 `&` 连接剩余关键词。

例如：

```text
用户问题：Commit Message 格式要求是什么？
查询文本：Commit & Message & 格式 & 要求
```

这里的 `&` 表示 AND 查询，通常要求这些词都匹配。

### 第 5 步：执行全文匹配

仓储层把查询文本转换为 `tsquery`：

```python
query_expr = func.to_tsquery("simple", query_text)
```

然后用 `@@` 判断 chunk 是否匹配：

```sql
content_tsv @@ query_expr
```

同时必须继续使用项目的安全过滤条件：

```text
kb_id 在当前授权范围内
chunk 属于当前文档版本
文档状态为 DONE
文档未删除
```

### 第 6 步：使用 `ts_rank` 排序

当前代码计算：

```python
rank_expr = ts_rank(DocChunk.content_tsv, query_expr)
```

然后：

```sql
ORDER BY rank_expr DESC
LIMIT top_k
```

`ts_rank` 是 PostgreSQL 自己的相关性评分。它会考虑匹配词项的频率，也可以配置字段权重和归一化方式；但它不是 BM25 公式。

本项目当前没有显式传入 normalization 参数，因此使用 PostgreSQL 的默认行为。

### 第 7 步：和向量检索做 RRF

全文检索返回一份已经排序的列表，向量检索也返回一份已经排序的列表：

```text
vector_hits = 向量 TopK
fulltext_hits = ts_rank 全文 TopK
```

随后使用：

```python
rrf_fuse(
    {
        "vector": vector_hits,
        "fulltext": fulltext_hits,
    },
    rrf_k=60,
)
```

RRF 只使用排名：

```text
RRF 分数 = 1 / (k + 排名)
```

因此，`ts_rank` 的原始分数不会直接和向量相似度相加。它首先影响全文结果的内部顺序，之后 RRF 再使用这个顺序。

## 二、如果改用 BM25，需要增加什么

BM25 也是关键词检索，但它使用另一套排序公式。它通常需要维护以下统计信息：

| 统计信息 | 含义 |
|---|---|
| `tf` | 一个词在当前文档中出现了多少次 |
| `df` | 一个词出现在多少个文档中 |
| `N` | 文档总数 |
| `dl` | 当前文档长度 |
| `avgdl` | 所有文档的平均长度 |

BM25 的简化公式是：

```text
BM25(document, query)
  = 对每个查询词求和：
    IDF(词) * 词频饱和项 * 文档长度归一化项
```

常见的完整形式为：

```text
score(D, Q) = Σ IDF(t) *
  [tf(t,D) * (k1 + 1)] /
  [tf(t,D) + k1 * (1 - b + b * dl / avgdl)]
```

初学时只需要理解三点：

1. 罕见词比常见词更重要；
2. 同一个词重复很多次，分数不会无限增加；
3. 文档过长会被适当惩罚。

### BM25 第 1 步：选择分析器

文档和查询必须使用同一套分析器：

```text
文档分词方式 = 查询分词方式
```

如果中文没有正确分词，BM25 也不能自动解决中文问题。因此，改 BM25 前要先决定中文分词、英文大小写、数字、连字符、同义词和停用词规则。

### BM25 第 2 步：创建 BM25 倒排索引

BM25 需要一个能维护词项和文档统计信息的倒排索引。工程上通常有两条路线：

```text
路线 A：PostgreSQL BM25 扩展
路线 B：Elasticsearch / OpenSearch 等独立搜索引擎
```

不建议把所有 chunk 读到 Python 再计算 BM25，那只能做小规模实验，不能作为生产召回方案。

### BM25 第 3 步：索引字段和权限元数据

BM25 索引至少要能够返回：

```text
chunk_id
doc_id
kb_id
document_name
content
page_num
section_title
doc_version
document_status
is_deleted
```

检索时必须在 BM25 查询内部硬过滤：

```text
kb_id 在已授权范围内
当前文档版本
DONE 状态
未删除
```

不能先查询全库 TopK，再在应用层删除无权限结果，因为这样可能导致有权限的结果已经被 TopK 截断。

### BM25 第 4 步：执行 BM25 查询

BM25 查询大致经过：

```text
用户问题
  -> 同样的分析器分词
  -> 查询倒排索引
  -> 找到包含查询词的候选 chunk
  -> 读取 tf、df、文档长度等统计信息
  -> 计算 BM25 分数
  -> 按分数降序返回 TopK
```

BM25 返回的仍然应该是项目统一的 `ChunkSearchHit` 列表，只是 `score` 变成 BM25 分数。

### BM25 第 5 步：接入现有 RRF

改用 BM25 后，RRF 流程不需要改变：

```text
vector_hits = PGVector 向量 TopK
bm25_hits = BM25 关键词 TopK
fused_hits = RRF(vector_hits, bm25_hits)
```

不要这样做：

```text
向量相似度 + BM25 原始分数
```

两种分数的量纲不同，不能直接相加。当前项目使用 RRF 正是为了避免这个问题。

## 三、`ts_rank` 和 BM25 的实现差异

| 阶段 | `ts_rank` | BM25 |
|---|---|---|
| 文档预处理 | `to_tsvector` | BM25 分析器和倒排索引 |
| 索引 | PostgreSQL GIN/GiST | BM25 扩展或独立搜索引擎索引 |
| 匹配 | `tsquery` + `@@` | 倒排列表召回 |
| 排序 | PostgreSQL `ts_rank` | BM25 公式 |
| 统计数据 | PostgreSQL 内部处理 | 需要维护 `tf`、`df`、`dl`、`avgdl` |
| 部署 | PostgreSQL 原生 | 扩展或额外搜索服务 |
| 现有 RRF | 直接复用 | 直接复用 |

最重要的区别是：

```text
ts_rank：现有 PostgreSQL 全文索引 + PostgreSQL 排序
BM25：BM25 倒排索引 + BM25 排序公式
```

BM25 不是把 `ts_rank(...)` 改名，也不是创建 GIN 索引后自动获得的能力。

## 四、本项目如果要实现 BM25，推荐怎么选

### 方案一：继续使用 `ts_rank`

适合：

- 先完成系统闭环；
- 文档规模中小；
- 全文检索只是向量检索的补充；
- 不希望增加新的扩展或搜索服务。

优点是改动和运维成本最低。

### 方案二：PostgreSQL BM25 扩展

适合：

- 希望继续让业务数据、向量和关键词索引在 PostgreSQL 内；
- 部署环境允许安装扩展；
- 不想维护 Elasticsearch 的同步链路。

需要先验证：

- PostgreSQL 版本是否支持；
- Docker 镜像或云数据库是否允许安装；
- 扩展是否支持当前的过滤条件和字段查询；
- 中文分析器是否满足要求。

### 方案三：Elasticsearch/OpenSearch

适合：

- 全文检索是核心能力；
- 需要成熟的中文分词、字段权重、短语查询、模糊查询和高吞吐；
- 可以接受额外服务、索引同步和运维成本。

如果选择该方案，数据库和搜索引擎之间需要处理：

```text
新增 chunk
更新 chunk
发布新文档版本
删除旧版本
索引失败重试
索引状态监控
```

## 五、给当前项目的实际建议

当前代码先不要删除 `ts_rank`。建议按以下顺序推进：

1. 把现有 `ts_rank` 作为全文检索基线；
2. 先改善中文分词和查询词构造；
3. 如果确实需要 BM25，增加一个独立的 `Bm25Retriever` 适配器；
4. 保持它返回与现有全文检索相同的 `ChunkSearchHit`；
5. 让 `HybridRetriever` 在配置下选择 `ts_rank` 或 BM25；
6. 保持 RRF、权限过滤、Reranker 和引用构建不变；
7. 用同一评估集比较两条链路：

```text
向量 + ts_rank + RRF
向量 + BM25 + RRF
```

重点比较：

- 关键词查询 Recall@K；
- 混合检索 Hit Rate 和 MRR；
- 条款号、配置项、英文缩写和编号查询；
- 中文查询效果；
- P95 延迟；
- 索引空间和维护成本。

最终判断标准不是“BM25 理论上更先进”，而是它在本项目真实评估数据上，是否明显改善了 RRF 之前的关键词召回和排序。

## 六、最终结论

```text
当前项目：PostgreSQL 全文检索 + ts_rank + RRF

如果使用 BM25：
  需要 BM25 扩展或独立搜索引擎
  不一定需要 Elasticsearch
  RRF、权限过滤和 Reranker 可以继续复用
```

如果部署环境支持可靠的 PostgreSQL BM25 扩展，优先评估“同库 BM25”；如果扩展不可用，或者全文检索需求明显变复杂，再考虑 Elasticsearch/OpenSearch。
