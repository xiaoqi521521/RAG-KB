# 07-文档增量更新——版本管理与差量重索引 Spec

本文基于 [07-文档增量更新——版本管理与差量重索引-plan.md](/D:/Code/python/Practical_Project/rag-kb/docs/plans/07-文档增量更新——版本管理与差量重索引-plan.md) 和参考资料 [11-文档增量更新——版本管理与差量重索引.md](/D:/Code/python/Practical_Project/rag-kb/docs/references/11-文档增量更新——版本管理与差量重索引.md)，定义 Python / FastAPI 版本的文档替换与强制重建接口技术方案。

本阶段只设计新增路由层能力：文档替换接口和强制重建接口。当前 `app/api/routes/knowledge_bases.py` 中已有的 `reindex_document` 路由暂不处理，不删除、不改名、不迁移，后续如果要统一 API 命名再单独做 Sync 或重构计划。

## 2.1 任务背景

前序阶段已经具备知识库文档上传、状态查询、MinIO 存储、索引任务、版本化 chunk 写入和 `IndexService.reindex_document(...)` 等基础能力。现在需要补齐参考资料第 11 章中的两个文档更新入口：

- 替换文档内容：用户上传新版本文件，保持原 `doc_id` 不变，更新文档元数据和 `minio_path`，再触发新版本索引。
- 强制重建索引：文件字节不变，只因解析、分块、Embedding 缓存版本或索引失败恢复等原因，重新生成 chunk 和向量。

核心目标：

- 保持文档业务身份稳定，替换文件时不创建新的 `kb_document`。
- 新版本索引完成前旧版本 chunk 继续可用，避免查询空窗。
- 重建前清理旧统计字段，避免前端轮询时误判新版本已经完成。
- 两个新接口都要求知识库写权限，并校验 `doc_id` 属于 `kb_id`。
- 不做页级或段落级 diff，差量收益由 `EmbeddingService` 的文本 hash 缓存承担。

当前技术前提：

```plain
app/services/indexing.py
  -> reindex_document(doc_id) 可创建 REINDEX 任务
  -> REINDEX 执行时会写入 current_version + 1 的 chunk
  -> 新版本写入完成后调用 delete_older_versions(...)

app/services/knowledge_base.py
  -> 已有文件类型、文件大小、文档归属校验可参考
  -> 已有 upload_document / get_index_status / delete_document 等文档管理流程

app/integrations/minio.py
  -> 已有 upload / download / delete
  -> delete 失败只记录告警，不阻断主流程

app/repositories/documents.py
  -> 需要新增替换文件元数据和重置统计字段的方法
```

## 2.2 范围对齐

### In Scope

本阶段技术交付物：

- 新增路由模块 `app/api/routes/document_updates.py`。
- 新增服务模块 `app/services/document_update.py`。
- 新增或扩展响应模型，推荐放在 `app/schemas/knowledge_base.py` 或新增 `app/schemas/document_update.py`。
- 扩展 `DocumentRepository`，支持：
  - 校验并获取未删除文档。
  - 替换文档文件元数据。
  - 重建前重置状态、错误信息和索引统计字段。
- 新增接口：
  - `PUT /api/v1/kb/{kb_id}/documents/{doc_id}/content`
  - `POST /api/v1/kb/{kb_id}/documents/{doc_id}/reindex-force`
- 两个接口都调用 `IndexService.reindex_document(doc_id)` 创建 `REINDEX` 任务。
- 补充相关单元测试或 API 测试，覆盖权限、文档归属、MinIO 补偿、状态重置和任务提交。

### Out of Scope

本阶段不做：

- 不处理 `app/api/routes/knowledge_bases.py` 已有的 `reindex_document` 接口，包括不删除、不重命名、不改响应。
- 不实现在线查询链路的当前版本过滤；这里只在 Spec 中保留约束。
- 不实现文档版本列表、历史版本查看、版本回滚或可视化 diff。
- 不实现单页、单段、单 chunk 的局部重建。
- 不改变 `EmbeddingService` 缓存 key 规则。
- 不引入 Celery / Dramatiq 等任务队列。
- 不改数据库表结构，除非实现时发现现有字段无法表达本阶段状态。

## 2.3 输入/输出模型

### 2.3.1 文档替换接口

```plain
PUT /api/v1/kb/{kb_id}/documents/{doc_id}/content
Content-Type: multipart/form-data
```

Path 参数：

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `kb_id` | int | 目标知识库 ID，必须存在且当前用户具备写权限 |
| `doc_id` | int | 待替换文档 ID，必须存在、未删除且属于 `kb_id` |

Form 参数：

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `file` | UploadFile | 必填；文件名不能为空；扩展名仅允许 `.pdf`、`.docx`、`.md`、`.txt`；大小不得超过 `settings.max_upload_file_size_mb` |

建议响应：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "doc_id": 1,
    "file_name": "updated.pdf",
    "status": "PENDING",
    "task_id": 12,
    "message": "文档内容已替换，重建索引任务已提交，请通过 status 接口查询进度"
  }
}
```

建议 DTO：

```python
class DocumentReindexSubmitResponse(BaseModel):
    doc_id: int
    file_name: str
    status: str
    task_id: int
    message: str
```

持久化结果：

- `kb_document.id` 不变。
- `kb_document.kb_id` 不变。
- `kb_document.file_name` 更新为新文件名。
- `kb_document.file_type` 根据新文件名重新识别。
- `kb_document.file_size` 更新为新文件大小。
- `kb_document.minio_path` 更新为新对象 key。
- `kb_document.status = PENDING`。
- `kb_document.error_msg = None`。
- `kb_document.chunk_count = None` 或 `0`，需在实现中统一。
- `kb_document.token_count = None` 或 `0`，需与 `chunk_count` 策略一致。
- `kb_document.indexed_at = None`。
- 创建一条 `kb_index_task(task_type=REINDEX, status=PENDING)`。

错误响应：

| 状态码 | 场景 |
| --- | --- |
| 400 | 文件名为空、文件类型不支持、文件大小超过限制 |
| 403 | 当前用户没有目标知识库写权限 |
| 404 | 文档不存在、已删除，或不属于该知识库 |
| 500 | MinIO 上传失败、数据库更新失败、任务提交失败等未预期错误 |

### 2.3.2 强制重建接口

```plain
POST /api/v1/kb/{kb_id}/documents/{doc_id}/reindex-force
```

Path 参数：

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `kb_id` | int | 目标知识库 ID，必须存在且当前用户具备写权限 |
| `doc_id` | int | 待重建文档 ID，必须存在、未删除且属于 `kb_id` |

请求体：

```plain
无
```

建议响应：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "doc_id": 1,
    "file_name": "handbook.pdf",
    "status": "PENDING",
    "task_id": 13,
    "message": "强制重建索引任务已提交，请通过 status 接口查询进度"
  }
}
```

持久化结果：

- 不更新 `file_name`、`file_type`、`file_size`、`minio_path`。
- `kb_document.status = PENDING`。
- `kb_document.error_msg = None`。
- 清理旧 `chunk_count`、`token_count`、`indexed_at`。
- 创建一条 `kb_index_task(task_type=REINDEX, status=PENDING)`。

错误响应：

| 状态码 | 场景 |
| --- | --- |
| 403 | 当前用户没有目标知识库写权限 |
| 404 | 文档不存在、已删除，或不属于该知识库 |
| 409 | 文档当前已处于 `PENDING` 或 `PROCESSING`，拒绝重复提交重建 |
| 500 | 任务提交失败或其他未预期错误 |

### 2.3.3 与现有状态接口的关系

本阶段不新增状态查询接口。前端继续使用已有：

```plain
GET /api/v1/kb/{kb_id}/documents/{doc_id}/status
```

替换或强制重建提交后，状态接口应能读到：

- `status = PENDING / PROCESSING / DONE / FAILED`
- `retry_count`
- 新版本完成后的 `chunk_count`、`token_count`、`indexed_at`
- 失败时的 `error_msg`

## 2.4 代码执行流程

### 2.4.1 总体调用链路

文档替换：

```plain
document_updates.replace_content route
  -> PermissionService.require_write(kb_id, user)
  -> DocumentUpdateService.replace_content(kb_id, doc_id, file, user)
  -> DocumentRepository.get_active_in_kb(kb_id, doc_id)
  -> 校验文件名、类型、大小和文档状态
  -> MinioStorageService.upload(kb_id, file)
  -> DocumentRepository.replace_file_and_reset_index(...)
  -> IndexService.reindex_document(doc_id)
  -> 返回 DocumentReindexSubmitResponse
```

强制重建：

```plain
document_updates.force_reindex route
  -> PermissionService.require_write(kb_id, user)
  -> DocumentUpdateService.force_reindex(kb_id, doc_id)
  -> DocumentRepository.get_active_in_kb(kb_id, doc_id)
  -> 校验文档状态
  -> DocumentRepository.reset_for_reindex(doc_id)
  -> IndexService.reindex_document(doc_id)
  -> 返回 DocumentReindexSubmitResponse
```

后台索引链路继续复用现有：

```plain
IndexService.reindex_document(doc_id)
  -> 创建 REINDEX 任务
  -> commit_before_launch()
  -> _launch_task(task_id, doc_id)
  -> run_task(...)
  -> 下载当前 document.minio_path
  -> 解析、分块、Embedding
  -> 写入 new_version chunk
  -> mark_done(version=new_version)
  -> delete_older_versions(doc_id, new_version)
```

### 2.4.2 新路由模块

建议新增：

```plain
app/api/routes/document_updates.py
```

职责：

- 只承接文档更新类 HTTP 入口。
- 使用与 `knowledge_bases.py` 相同的 `get_current_user`、`get_permission_service` 和数据库会话依赖。
- 路由前缀仍挂在 `/api/v1/kb` 下，避免 API 路径割裂。
- 不在路由层读写数据库，不直接操作 MinIO。

建议两个 route function：

```python
async def replace_document_content(...)
async def force_reindex_document(...)
```

每个函数必须有 Docstring，说明职责、关键参数和返回结构。

### 2.4.3 新服务模块

建议新增：

```plain
app/services/document_update.py
```

职责：

- 编排文档替换和强制重建。
- 复用现有文件类型和大小校验逻辑；如果实现时复制自 `KnowledgeBaseService`，应优先抽成内部私有方法，避免行为不一致。
- 在提交索引任务前完成文档状态重置。
- 处理 MinIO 新文件上传后的失败补偿。

推荐服务方法：

```python
async def replace_content(
    self,
    kb_id: int,
    doc_id: int,
    file: UploadFile,
    user: CurrentUser,
) -> DocumentReindexSubmitResponse
```

```python
async def force_reindex(
    self,
    kb_id: int,
    doc_id: int,
) -> DocumentReindexSubmitResponse
```

Python / SQLAlchemy 没有 Spring AOP self-invocation 陷阱，因此不需要照搬参考资料里的 `DocumentTxService` 双 Bean 结构。但仍要明确事务顺序：数据库更新和任务创建必须在后台任务启动前提交，否则后台独立会话可能读不到最新 `minio_path` 或任务记录。

### 2.4.4 Repository 方法

建议扩展：

```plain
app/repositories/documents.py
```

新增方法一：

```python
async def get_active_in_kb(self, kb_id: int, doc_id: int) -> KbDocument | None
```

职责：

- 查询 `id = doc_id`。
- 要求 `is_deleted = False`。
- 要求 `kb_id` 匹配。
- 不匹配时返回 `None`，由服务层统一转为 404。

新增方法二：

```python
async def replace_file_and_reset_index(
    self,
    doc_id: int,
    *,
    file_name: str,
    file_type: str,
    file_size: int,
    minio_path: str,
) -> KbDocument
```

职责：

- 更新文件元数据和 MinIO 路径。
- 重置状态为 `PENDING`。
- 清空 `error_msg`。
- 清理 `chunk_count`、`token_count`、`indexed_at`。
- 不在这里递增 `version`；版本递增由 `IndexService` 的 `REINDEX` 执行阶段统一处理。

新增方法三：

```python
async def reset_for_reindex(self, doc_id: int) -> None
```

当前已有该方法，但需要增强为：

- `status = PENDING`
- `error_msg = None`
- `chunk_count = None` 或 `0`
- `token_count = None` 或 `0`
- `indexed_at = None`

清理统计字段的具体值建议统一使用 `None`，因为它更明确表达“本轮索引尚未产生统计结果”。如果现有数据库默认值和响应模型更适合 `0`，实现时必须保持 `chunk_count` 和 `token_count` 一致，并在测试中固定。

### 2.4.5 状态与并发边界

两个新接口都应拒绝重复提交：

```plain
document.status in {"PENDING", "PROCESSING"}
  -> HTTP 409
  -> 不上传新文件
  -> 不创建新任务
```

原因：

- 避免两个 REINDEX 任务并发写入不同版本。
- 避免重复消耗 Embedding 成本。
- 避免替换文档时新旧 MinIO 对象指向混乱。

允许提交的状态：

```plain
DONE
FAILED
```

`FAILED` 允许再次提交，是为了支持人工恢复。

### 2.4.6 异常处理和补偿

文档替换的关键补偿：

```plain
MinIO 上传新文件成功
  -> 数据库更新或任务提交失败
  -> 调用 MinioStorageService.delete(new_minio_path)
  -> 如果已有 document 可安全标记失败，则写入 FAILED 和 error_msg
  -> 继续向上抛出异常
```

旧 MinIO 文件删除策略：

```plain
数据库已切换到 new_minio_path
  -> REINDEX 任务已成功提交
  -> 可以删除 old_minio_path
```

当前 `MinioStorageService.delete(...)` 失败只记录告警，不阻断主流程。由于后台任务读取的是新 `minio_path`，删除旧对象不会影响索引执行。

强制重建没有新文件上传，因此不需要 MinIO 补偿。

任务提交失败：

- 如果发生在状态重置之后，应把文档标记为 `FAILED` 并记录错误。
- 不删除旧 chunk。
- 替换场景中，若数据库已切换到新 `minio_path` 但任务提交失败，状态接口会显示 `FAILED`，用户可再次触发强制重建。

## 2.5 技术约束与最佳实践

- 权限校验必须在路由入口执行：`permission_service.require_write(kb_id, user)`。
- 服务层必须再次校验文档归属，不能只依赖路由传参。
- 新接口不直接暴露 SQLAlchemy ORM 对象，统一返回 Pydantic DTO。
- 不把权限、版本或文档状态约束放进 Prompt。
- 不手动操作 chunk 版本号；版本递增和旧版本清理继续由 `IndexService` 负责。
- 不把 `INDEX` 任务用于文档替换或强制重建；两者都必须创建 `REINDEX` 任务。
- MinIO SDK 是同步客户端，继续通过 `asyncio.to_thread(...)` 包裹，避免阻塞事件循环。
- 文件大小校验使用 `UploadFile.size` 时要注意可能为 `None`；如果为 `None`，实现应选择读取内容后计算或拒绝请求，不能默默按 0 处理大文件。
- 日志至少记录 `kb_id`、`doc_id`、`task_id`、新旧 `minio_path`、操作类型和失败原因。
- 错误信息写入 `error_msg` 时要控制长度，避免数据库 `Text` 中堆入完整堆栈；完整异常堆栈交给日志。
- 本阶段不新增数据库迁移；如果实现中发现 `chunk_count/token_count/indexed_at` 不能置空，应同步更新 Spec 并说明原因。

## 2.6 验收标准

### 功能行为

- `PUT /api/v1/kb/{kb_id}/documents/{doc_id}/content` 成功后，返回 `doc_id`、新 `file_name`、`status=PENDING` 和 `task_id`。
- 替换文档不会创建新的 `kb_document` 记录，原 `doc_id` 保持不变。
- 替换文档会更新 `file_name`、`file_type`、`file_size`、`minio_path`。
- 替换文档会创建 `REINDEX` 任务，而不是 `INDEX` 任务。
- `POST /api/v1/kb/{kb_id}/documents/{doc_id}/reindex-force` 成功后，不更新文件元数据和 `minio_path`。
- 强制重建会创建 `REINDEX` 任务，并把文档状态重置为 `PENDING`。
- 两个接口提交后，已有 status 接口能返回最新任务的 `retry_count` 和文档状态。

### 异常分支

- 无写权限访问两个接口时返回 403，且不上传文件、不改数据库、不创建任务。
- `doc_id` 不存在、已删除或不属于 `kb_id` 时返回 404。
- 文档处于 `PENDING` 或 `PROCESSING` 时返回 409，且不创建新任务。
- 替换接口收到空文件名或不支持扩展名时返回 400。
- 替换接口中文件超过大小限制时返回 400。
- 替换接口在新 MinIO 上传成功但后续失败时，会调用 `delete(new_minio_path)` 做补偿。
- 任务提交失败时，文档进入 `FAILED` 并记录 `error_msg`，旧 chunk 不删除。

### 权限与数据边界

- 两个接口都必须使用 `require_write`，不能使用 `require_read`。
- 服务层必须通过 `kb_id + doc_id + is_deleted=False` 校验文档，避免跨知识库替换。
- 管理员和显式 WRITE / ADMIN 权限用户可操作；仅 READ 权限用户不可操作。

### 日志与可观测性

- 替换成功日志包含 `kb_id`、`doc_id`、`task_id`、`old_minio_path`、`new_minio_path`。
- 强制重建成功日志包含 `kb_id`、`doc_id`、`task_id`。
- 失败日志包含操作类型和失败阶段。
- 本阶段不强制新增 Prometheus 指标；如新增，需同步更新 Spec。

### 测试命令

实现完成后至少运行：

```powershell
uv run pytest tests/services/test_document_update.py -v
uv run pytest tests/api/test_document_updates.py -v
uv run pytest tests/services/test_indexing.py -v
```

如果测试文件尚不存在，应在实现阶段创建。若项目测试环境无法访问真实 MinIO，应使用 fake storage 验证业务行为，不在生产代码里添加测试专用分支。

### 文档同步

- 本文件名必须保持 `*-spec.md` 后缀：`07-文档增量更新——版本管理与差量重索引-spec.md`。
- 本阶段 Plan 已存在：`docs/plans/07-文档增量更新——版本管理与差量重索引-plan.md`。
- 如果实现时决定修改现有 `knowledge_bases.reindex_document` 路由，必须同步更新本 Spec，因为当前范围明确暂不处理该接口。
- 代码实现完成后，需要新增或更新对应 Process 文档，说明实际文件变更和最终执行流程。
