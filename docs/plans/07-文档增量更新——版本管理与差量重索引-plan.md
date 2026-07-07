# 07-文档增量更新——版本管理与差量重索引 Plan

本文基于 [11-文档增量更新——版本管理与差量重索引.md](/D:/Code/python/Practical_Project/rag-kb/docs/references/11-文档增量更新——版本管理与差量重索引.md) 梳理 LangChain / FastAPI 版本中的文档更新、版本管理与差量重索引计划。

本阶段关注“文档内容发生变化后，如何保持文档 ID 稳定、尽量减少查询影响、Embedding 成本可控、索引状态可追踪”。它不是重新设计完整索引管道，也不做复杂段落级 diff。当前项目已经具备 `IndexService.reindex_document(...)`、文档管理路由和版本化 chunk 写入基础，因此本文重点明确下一步需要补齐的业务流程、边界和验收方向。

## 1.1 任务背景

企业知识库中的文档并不是一次上传后永不变化。常见场景包括：

- HR 手册、制度文件、产品 FAQ 等文档被替换为新版本。
- 文档原文没有变化，但解析器、分块策略或 Embedding 模型配置变化，需要强制重建索引。
- 索引任务失败后，运营人员希望重新触发索引，而不重新创建文档记录。

如果每次文档更新都先删除旧 chunk 再重新索引，会带来两个明显问题：

- 新索引失败或耗时较长时，线上查询会短暂查不到该文档。
- 大部分内容未变化时，全部重新调用 Embedding API 会增加延迟和 Token 成本。

参考文献给出的核心取舍是：生产上优先支持“整文档替换 + 版本化重建 + Embedding 缓存复用”。也就是说，本阶段不做精细到页、段落或 chunk 的内容 diff，而是在重新解析和重新分块后，依赖 `EmbeddingService` 的内容 hash 缓存复用未变化 chunk 的向量。这样可以在复杂度可控的前提下，同时保证：

- 文档 ID 不变，前端和引用关系更稳定。
- 新版本索引完成前旧版本 chunk 仍然可用；已发布文档重建期间保持 `kb_document.status = DONE`，重建进度由 `kb_index_task` 承载。
- 内容相同的 chunk 命中 Redis embedding 缓存，不重复调用外部模型。
- 强制重建索引可以复用同一套版本切换流程。

当前代码基础：

```plain
app/services/indexing.py
  -> 已有 submit_index_task(doc_id) 和 reindex_document(doc_id)
  -> 已有 INDEX / REINDEX 任务类型
  -> REINDEX 会在写入 chunk 时递增 document.version
  -> 新版本 chunk 写入完成后调用 delete_older_versions(...)

app/services/knowledge_base.py
  -> 已有 reindex_document(kb_id, doc_id)
  -> 会先 reset_for_reindex(doc_id)，再提交重建任务

app/api/routes/knowledge_bases.py
  -> 已有 POST /{kb_id}/documents/{doc_id}/reindex
  -> 已有上传、状态查询、文档列表、下载、删除接口基础

app/repositories/documents.py
  -> 已有 reset_for_reindex(doc_id)
  -> 目前只重置 status 和 error_msg，尚未清理旧统计字段
```

因此本阶段的业务目标不是“从零添加重建接口”，而是把文档替换、强制重建、版本切换、缓存复用、失败回滚和状态展示这几块对齐成完整的企业级更新流程。

## 1.2 业务流程

### 1.2.1 总体执行链路

文档替换流程：

```plain
1. 校验更新权限：用户必须对目标 kb_id 具备 WRITE / ADMIN 权限
2. 校验文档归属：doc_id 必须存在、未删除，并且属于传入的 kb_id
3. 保存新原文：上传新文件到 MinIO，得到 new_minio_path
4. 更新文档记录：保持 doc_id 不变，更新文件名、大小、类型、minio_path，重置状态和统计字段
5. 提交重建任务：创建 REINDEX 任务，后台执行解析、分块、Embedding 和 chunk 入库
6. 版本切换：新版本 chunk 全部写入成功后，更新 document.version 和索引统计
7. 清理旧资源：删除旧版本 chunk；确认新索引可用后再删除旧 MinIO 原文
8. 状态可见：前端通过 status 接口看到 PENDING / PROCESSING / DONE / FAILED 和失败原因
```

强制重建流程：

```plain
1. 校验写权限和文档归属
2. 不替换 MinIO 原文，只重置文档状态、错误信息和旧统计字段
3. 创建 REINDEX 任务
4. 后台从当前 minio_path 读取原文
5. 使用当前解析、分块和 Embedding 配置重新索引
6. 新版本 chunk 写入成功后递增版本并清理旧版本 chunk
7. 通过 status 接口返回最新状态、chunk 数、token 数、重试次数和错误信息
```

### 1.2.2 完整业务流程

#### 文档替换入口

文档替换是“保留文档业务身份，替换文档内容”。它不同于删除旧文档后重新上传新文档：

- `doc_id` 保持不变，便于前端列表、审计记录和未来引用关系继续追踪。
- `kb_id` 不允许变化，避免借更新接口把文档移动到无权限知识库。
- 上传人可以记录为最后更新人，但不改变文档所属知识库。
- 新文件必须重新校验类型、大小和空文件边界。

入口层应先完成权限硬校验，再进入业务服务。权限不能只写入 Prompt 或靠前端隐藏按钮实现。对于文档替换和强制重建，权限级别应与上传、删除一致：需要知识库写权限。

#### 原文存储与事务边界

替换文件时会同时碰到数据库和 MinIO 两类资源。计划采用“先上传新文件，再更新数据库，再提交重建任务”的主流程，但必须处理失败补偿：

```plain
新文件上传成功
  -> 数据库更新失败
     -> 删除新 MinIO 对象，保留旧文档记录和旧索引

数据库更新成功
  -> 重建任务提交失败
     -> 文档标记 FAILED，保留新 minio_path，允许人工再次提交重建

重建任务执行失败
  -> 新版本 chunk 不应成为可检索版本
  -> 旧版本 chunk 继续保留
  -> status 接口展示 FAILED 和 error_msg
```

旧 MinIO 原文不应在数据库更新前删除，也不应在新索引尚未提交前删除。更稳妥的业务策略是：数据库成功切到新 `minio_path` 且重建任务成功后，再异步删除旧对象；如果旧对象删除失败，只记录告警并允许后台清理，不影响新版本可用。

#### 版本号与 chunk 生命周期

版本字段是避免半成品 chunk 被查询到的关键。在线 RAG 检索同时过滤 `kb_document.status = DONE`，因此已发布文档重建期间必须保持 `DONE`，只让 `kb_index_task` 表达重建进度。

```plain
首次索引：
  document.version = 1
  chunk.doc_version = 1

重建索引：
  读取 current_version = document.version
  new_version = current_version + 1
  新 chunk 写入 doc_version = new_version
  新 chunk 全部写入成功后，document.version = new_version
  删除 doc_version < new_version 的旧 chunk
```

这个流程有两个业务要求：

- 新版本未完成前，旧版本 chunk 仍可查询。
- 检索 SQL 后续必须使用 `doc_version = kb_document.version` 或等价条件过滤当前版本，避免召回半成品版本。

当前 `IndexService` 已经采用“REINDEX 时版本号 +1，新 chunk 写入后再 mark_done，再删除旧版本”的方向。后续 Spec 需要继续确认清理旧版本失败时的状态策略：旧 chunk 残留不应导致任务失败回滚新版本，但需要记录日志和可补偿清理。

#### Embedding 缓存复用

本阶段不做局部 diff，不逐页判断哪些内容变更。差量收益来自 Embedding 层的内容 hash 缓存：

```plain
重新解析和重新分块
  -> 对每个 chunk 文本计算缓存 key
  -> Redis 命中：直接复用向量
  -> Redis 未命中：调用 Embedding API
  -> 向量按 chunk 顺序入库到新版本
```

需要在业务计划中明确一个边界：缓存命中率不是简单等于“未修改内容比例”。如果使用滑动窗口分块，文档开头新增内容可能导致后续 chunk 边界整体漂移，命中率会显著下降；结构感知分块在章节稳定时更容易复用缓存。后续优化分块策略时，应把“版本更新场景下的缓存命中率”作为质量指标之一。

#### 状态流转与前端可见性

文档替换或强制重建后，用户看到的状态不能混淆旧统计和新任务状态。计划状态流转如下：

```plain
用户提交替换/重建
  -> 已发布 document.status 保持 DONE
  -> 新文件元数据暂存到 index_task.payload
  -> index_task.status = PENDING

后台开始执行
  -> index_task.status = RUNNING

执行成功
  -> document.status = DONE
  -> document.version = new_version
  -> document.chunk_count = 本次新版本 chunk 数
  -> document.token_count = 本次 token 估算
  -> document.indexed_at = 当前时间
  -> index_task.status = DONE

执行失败
  -> document.status = FAILED
  -> document.error_msg = 失败原因
  -> index_task.status = FAILED 或 PENDING(等待重试)
  -> 旧版本 chunk 不删除
```

当前 `reset_for_reindex(doc_id)` 只重置了 `status` 和 `error_msg`。后续应补齐统计字段清理，避免前端轮询时看到“状态 PENDING，但 chunk_count 仍是旧值”而误判新版本已经完成。

#### 自动重试与手动重试

文档替换与强制重建都基于持久化 MinIO 原文，因此适合复用索引任务的自动重试机制。失败分类沿用索引管道原则：

| 失败场景 | 处理策略 | 业务说明 |
| --- | --- | --- |
| 文档不存在或已删除 | 不自动重试 | 数据状态确定错误 |
| 无写权限 | 不创建任务 | 入口层直接拒绝 |
| MinIO 临时网络异常 | 自动重试 | 原文路径已持久化，可重新下载 |
| MinIO 对象不存在 | 不自动重试 | 数据不一致，需要人工处理 |
| 解析服务偶发异常 | 自动重试到上限 | 可能由临时依赖问题导致 |
| 文档无有效文本 | 不自动重试或快速失败 | 重试无法产生 chunk |
| Embedding 服务超时 | 自动重试 | 外部模型服务可能恢复 |
| DB 写入异常 | 自动重试 | 连接或事务冲突可能恢复 |

强制重建失败后，应允许用户再次点击重建。替换文档失败后，应保留新文件记录和失败状态，允许用户在不重复上传时重新触发索引；如果后续产品希望“一键回滚旧原文”，应作为独立功能设计，不纳入本阶段。

### 1.2.3 关键分支与边界条件

#### 分支一：替换整个文档

这是本阶段的主要场景。用户上传新文件，系统更新 `kb_document` 的文件信息和 `minio_path`，然后提交 `REINDEX` 任务。业务上保留原 `doc_id`，技术上重新解析和分块，成本优化依赖 Embedding 缓存。

#### 分支二：强制重建索引

文件字节不变，但索引策略变化。常见原因包括：

- 分块参数变化。
- 解析器升级。
- Embedding 模型或缓存版本变化。
- 旧索引失败后需要人工重新触发。

此分支不上传新原文，也不改文件名、大小、类型和 `minio_path`，只创建重建任务并进入版本化索引流程。

#### 分支三：旧版本清理失败

如果新版本已经写入成功且 `document.version` 已切换，旧 chunk 清理失败不应让用户查询回退为失败。业务上应记录日志和可观测事件，并提供后续后台清理能力。检索层只召回当前版本时，旧 chunk 残留不会影响回答质量，但会增加存储成本。

#### 分支四：替换期间用户查询

在线查询链路在本阶段不是实现重点，但 Plan 必须保留目标约束：替换或强制重建期间，理想行为是查询继续使用旧版本 chunk，只有当新版本任务成功完成后才切到新版本。

当前实现已按该目标调整：文档替换或强制重建不会把已发布文档状态重置为 `PENDING / PROCESSING`，`document.version` 继续指向旧版本 chunk；新文件元数据暂存到 `kb_index_task.payload`，待新版本索引成功后再发布。因此任务执行期间用户查询仍会命中旧版本内容。

#### 分支五：重复点击重建

同一个文档在 `PENDING` 或 `PROCESSING` 状态下重复触发重建，可能造成多个 REINDEX 任务并发写入不同版本。后续 Spec 需要选择明确策略：

- 保守策略：文档处于 `PENDING` / `PROCESSING` 时拒绝重复重建。
- 队列策略：允许创建任务但串行执行，每次只处理最新任务。

本阶段计划优先采用保守策略，避免版本竞争和重复 Embedding 成本。

## 1.3 业务边界

### In Scope

本阶段必须覆盖的业务范围：

- 明确文档替换流程：保持 `doc_id` 不变，新 MinIO 原文和文件元数据先进入任务 payload，提交 `REINDEX` 任务成功后再发布。
- 明确强制重建流程：不替换原文，只基于当前 `minio_path` 重新索引。
- 明确版本化 chunk 策略：新版本 chunk 完整写入成功后，再切换 `document.version` 并清理旧版本。
- 明确差量成本优化方式：不做精细 diff，依赖 Embedding 内容 hash 缓存复用未变化 chunk 的向量。
- 明确状态流转：首次索引仍按文档状态 `PENDING -> PROCESSING -> DONE / FAILED`；已发布文档 REINDEX 期间主文档保持 `DONE`，任务状态表达 `PENDING -> RUNNING -> DONE / FAILED`。
- 明确权限边界：替换和强制重建必须要求知识库写权限，并校验 `doc_id` 属于 `kb_id`。
- 明确失败处理：新文件上传、数据库更新、任务提交、索引执行和旧资源清理分别定义补偿或降级策略。
- 明确查询不中断目标：重建期间旧版本 chunk 保持可用，直到新版本完整发布。
- 对齐当前 Python 项目已有的 `IndexService`、`KnowledgeBaseService`、Repository 和路由能力。

### Out of Scope

本阶段明确不实现或不展开的内容：

- 不实现页级、段落级或 AST 级内容 diff。
- 不实现只重建单个 chunk 或单个页码的局部索引。
- 不实现文档版本对比、历史版本浏览、版本回滚 UI。
- 不改变 Embedding 缓存内部 key 规则；缓存版本升级策略由 Embedding 模块或后续 Spec 单独约束。
- 不实现在线查询链路，包括混合检索、RRF、Reranker、上下文裁剪和回答生成。
- 不实现前端页面、上传进度条、版本历史列表或可视化 diff。
- 不引入 Celery / Dramatiq 等生产任务队列作为本阶段必选项；如后续需要持久化任务调度，再单独规划。
- 不设计对象存储多版本保留策略；旧 MinIO 对象默认在新版本成功后删除或交给后台清理。
- 不实现租户字段改造；本阶段沿用现有 `kb_id` 权限隔离，后续多租户改造时再补齐 `tenant_id`。

## 1.4 与当前代码的衔接计划

当前代码已经完成了不少基础能力，下一步建议围绕“补齐替换入口”和“收紧重建边界”推进。

### 1.4.1 服务层衔接

建议在 `KnowledgeBaseService` 中新增“替换文档内容”的业务方法，职责类似上传文档和重建索引的组合：

```plain
replace_document_content(kb_id, doc_id, file, user)
  -> 校验文档存在且属于 kb_id
  -> 校验文件名、类型、大小
  -> 上传新文件到 MinIO
  -> 更新 kb_document 文件元数据、minio_path、status、error_msg、统计字段
  -> 提交 IndexService.reindex_document(doc_id)
  -> 返回文档当前状态或任务提交结果
```

这个方法不应直接执行解析、分块和向量化；索引细节继续收敛在 `IndexService`。

### 1.4.2 Repository 衔接

`DocumentRepository` 需要补齐两类能力：

- 替换文档文件元数据：更新 `file_name`、`file_type`、`file_size`、`minio_path`。
- 重置索引统计字段：重建前清理 `chunk_count`、`token_count`、`indexed_at` 和 `error_msg`。

当前 `reset_for_reindex(doc_id)` 可以作为起点，但需要避免旧统计误导前端。是否把 `indexed_at` 清空，后续 Spec 可根据前端展示选择：如果需要保留“上次成功索引时间”，更好的做法是新增独立字段，而不是复用当前 `indexed_at`。

### 1.4.3 API 衔接

已有重建接口：

```plain
POST /api/v1/kb/{kb_id}/documents/{doc_id}/reindex
```

建议新增文档替换接口：

```plain
PUT /api/v1/kb/{kb_id}/documents/{doc_id}/content
```

该接口与上传接口一样接收 multipart file，但语义上是“替换已有文档内容”。入口层必须执行写权限校验，服务层必须再次校验文档归属，避免用户通过 URL 组合替换其他知识库文档。

### 1.4.4 索引层衔接

`IndexService` 当前已经具备：

- `reindex_document(doc_id)` 创建 `REINDEX` 任务。
- `_resolve_doc_version(...)` 对 REINDEX 执行版本号递增。
- `_build_doc_chunks(...)` 写入 `doc_version`。
- 新版本写入后删除旧版本 chunk。

后续需要重点确认：

- 文档在 `PENDING` / `PROCESSING` 状态时是否拒绝重复重建。
- 分块为空是否应视为不可重试失败，避免无意义指数退避。
- 旧版本清理失败是否应从主任务失败中拆出为告警。
- `delete_older_versions(...)` 是否只按 `doc_id` 删除，还是需要带 `kb_id` 增强隔离。

## 1.5 验收方向

本 Plan 不直接修改代码，但后续 Spec 和实现应能验证以下行为：

- 替换文档提交后，`doc_id` 不变，`minio_path`、文件名、大小和类型在新版本索引成功后更新。
- 替换文档会创建 `REINDEX` 任务，而不是重新创建一条文档记录。
- 强制重建不改变 `minio_path`，但会创建 `REINDEX` 任务。
- 重建任务成功后，`document.version` 递增，新的 `DocChunk.doc_version` 与文档版本一致。
- 重建失败时，旧版本 chunk 不被删除；若文档原本已发布，主文档保持 `DONE`，旧版本继续可查询。
- 重建开始后，状态接口不会继续展示容易误解为新版本完成的旧统计数据。
- 无写权限用户不能替换文档或强制重建。
- `doc_id` 不属于 `kb_id` 时返回文档不存在或无权限，不泄露其他知识库文档信息。
- 内容相同的 chunk 能通过 Embedding 缓存复用向量；缓存命中率作为日志或指标记录方向。
- 本文档文件名符合 Plan 阶段要求，已使用 `-plan.md` 后缀。

## 1.6 下一步文档建议

完成本 Plan 后，下一份 Spec 应重点回答：

- `PUT /api/v1/kb/{kb_id}/documents/{doc_id}/content` 的请求、响应、错误码和权限依赖。
- `DocumentRepository` 替换文件元数据和重置统计字段的精确方法签名。
- 文档处于 `PENDING` / `PROCESSING` 时重复替换或重复重建的处理策略。
- 如何拆分“可查询发布状态”和“重建任务状态”，避免文档替换期间影响 RAG 查询。
- 新 MinIO 对象、旧 MinIO 对象和数据库事务失败时的补偿顺序。
- 旧版本 chunk 清理失败时，任务是否保持 DONE 并记录告警。
- 哪些测试覆盖权限、版本递增、旧版本保留、状态重置和缓存复用路径。
