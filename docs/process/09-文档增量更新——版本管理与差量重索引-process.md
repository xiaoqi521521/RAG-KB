# 07-文档增量更新——版本管理与差量重索引 Process

本文记录基于 [07-文档增量更新——版本管理与差量重索引-spec.md](/D:/Code/python/Practical_Project/rag-kb/docs/specs/07-文档增量更新——版本管理与差量重索引-spec.md) 落地文档替换与强制重建接口的实际实现过程，供后续 Review 和追溯使用。

## 3.1 文件变更清单

### 新增文件

- `app/api/routes/document_updates.py`：新增文档更新路由，提供替换文档内容和强制重建索引两个接口。
- `app/services/document_update.py`：新增文档更新服务，编排文档归属校验、状态重置、MinIO 文件替换、REINDEX 任务提交和失败补偿。
- `tests/api/test_document_updates.py`：新增路由层测试，验证两个新接口的写权限校验、请求转发和响应结构。
- `tests/services/test_document_update.py`：新增服务层测试，验证替换文件、强制重建、并发状态拒绝和任务提交失败处理。
- `docs/process/07-文档增量更新——版本管理与差量重索引-process.md`：当前 Process 文档。

### 修改文件

- `app/api/router.py`：注册 `document_updates.router`，路径前缀为 `/api/v1/kb`。
- `app/repositories/documents.py`：新增 `get_active_in_kb(...)`、`replace_file_and_reset_index(...)`，并增强 `reset_for_reindex(...)` 清理旧统计字段。
- `app/schemas/knowledge_base.py`：新增 `DocumentReindexSubmitResponse`，用于替换和强制重建接口响应。

### 删除文件

无。

### 关联文档同步

- 已新增 Plan：`docs/plans/07-文档增量更新——版本管理与差量重索引-plan.md`。
- 已新增 Spec：`docs/specs/07-文档增量更新——版本管理与差量重索引-spec.md`。
- 本次实现后新增当前 Process 文档。

## 3.2 实际代码执行流程

### 实际总体执行链路

文档替换：

```plain
PUT /api/v1/kb/{kb_id}/documents/{doc_id}/content
  -> require_write(kb_id, user)
  -> DocumentUpdateService.replace_content(...)
  -> get_active_in_kb(kb_id, doc_id)
  -> 拒绝 PENDING / PROCESSING 文档
  -> 校验文件名、类型、大小
  -> 上传新文件到 MinIO
  -> replace_file_and_reset_index(...)
  -> IndexService.reindex_document(doc_id)
  -> 删除旧 MinIO 对象
  -> 返回 doc_id / file_name / status / task_id
```

强制重建：

```plain
POST /api/v1/kb/{kb_id}/documents/{doc_id}/reindex-force
  -> require_write(kb_id, user)
  -> DocumentUpdateService.force_reindex(...)
  -> get_active_in_kb(kb_id, doc_id)
  -> 拒绝 PENDING / PROCESSING 文档
  -> reset_for_reindex(doc_id)
  -> IndexService.reindex_document(doc_id)
  -> 返回 doc_id / file_name / status / task_id
```

### 完整落地流程

路由层新增 `document_updates.py`，与现有 `knowledge_bases.py` 共用 `/api/v1/kb` 前缀，但不修改已有 `knowledge_bases.reindex_document`。两个新接口都先调用 `PermissionService.require_write(...)`，确保文档替换和强制重建不会被只读用户触发。

服务层新增 `DocumentUpdateService`。替换文档时先读取 `kb_id + doc_id + is_deleted=False` 匹配的文档，再拒绝正在 `PENDING` 或 `PROCESSING` 的文档，避免并发重建写入多个版本。文件校验通过后上传新对象，随后更新文档元数据并清空旧统计字段，再调用 `IndexService.reindex_document(doc_id)` 创建 `REINDEX` 任务。

如果新文件上传成功但数据库尚未切换就失败，会删除新 MinIO 对象做补偿。如果数据库已经切换到新 `minio_path` 后任务提交失败，不删除新对象，而是将文档标记为 `FAILED`，保留新原文，方便用户后续重新触发强制重建。

强制重建不上传文件、不修改 `minio_path` 和文件元数据，只重置状态与统计字段，然后提交 `REINDEX` 任务。

`DocumentRepository.reset_for_reindex(...)` 已增强为清理 `chunk_count`、`token_count`、`indexed_at`，避免前端看到上一轮索引统计后误判新任务已完成。

## 3.3 核心代码片段及讲解

### 路由入口

```python
@router.put(
    "/{kb_id}/documents/{doc_id}/content",
    status_code=status.HTTP_200_OK,
)
async def replace_document_content(...):
    await permission_service.require_write(kb_id, user)
    return ApiResponse.ok(await update_service.replace_content(kb_id, doc_id, file, user))
```

设计点：

- 权限硬校验放在路由入口。
- 路由只负责编排依赖和返回响应，不直接读写数据库或 MinIO。
- 响应使用 `DocumentReindexSubmitResponse`，不暴露 ORM 对象。

### 替换文档主流程

```python
document = await self._get_editable_document(kb_id, doc_id)
file_name = self._require_file_name(file)
file_type = self._detect_file_type(file_name)
file_size = self._require_file_size(file)
old_minio_path = document.minio_path

try:
    new_minio_path = await self.storage_service.upload(kb_id, file)
    document = await self.document_repository.replace_file_and_reset_index(...)
    document_replaced = True
    task_id = await self.index_service.reindex_document(doc_id)
except Exception as exc:
    if new_minio_path is not None and not document_replaced:
        await self.storage_service.delete(new_minio_path)
    if document_replaced:
        await self.document_repository.mark_failed(doc_id, str(exc))
    raise

await self.storage_service.delete(old_minio_path)
```

设计点：

- 先上传新文件，再切换文档记录。
- 只有数据库尚未切换时才删除新对象。
- 数据库已经切到新对象后，如果任务提交失败，保留新对象并标记失败，避免后续重建读不到原文。
- 旧文件删除放在任务提交成功后执行，删除失败由 `MinioStorageService.delete(...)` 记录告警，不阻断主流程。

### 仓储层状态重置

```python
def _reset_index_fields(self, document: KbDocument) -> None:
    document.status = DocumentStatus.PENDING.value
    document.error_msg = None
    document.chunk_count = None
    document.token_count = None
    document.indexed_at = None
```

设计点：

- 重建前清空旧统计字段，避免状态接口混淆新旧索引结果。
- 不在仓储层递增版本号；版本递增仍由 `IndexService` 的 `REINDEX` 执行流程统一处理。

## 3.4 验证结果

已执行：

```powershell
$env:PYTHONPATH='.'; uv run pytest tests/services/test_document_update.py tests/api/test_document_updates.py -v
```

结果：6 passed，1 个来自 `fastapi.testclient` 的 StarletteDeprecationWarning。

```powershell
$env:PYTHONPATH='.'; uv run pytest tests/api/test_knowledge_bases.py tests/services/test_knowledge_base_service.py tests/services/test_indexing.py -v
```

结果：26 passed，1 个来自 `fastapi.testclient` 的 StarletteDeprecationWarning。

```powershell
uv run ruff check app tests
```

结果：All checks passed。

```powershell
$env:PYTHONPATH='.'; uv run pytest -v
```

结果：77 passed，1 个来自 `fastapi.testclient` 的 StarletteDeprecationWarning。

```powershell
$env:PYTHONPATH='.'; uv run mypy app
```

结果：未通过，当前有 12 个既有类型问题，集中在：

- `app/services/document_loader/markdown_parser.py`
- `app/core/config.py`
- `app/core/exception_handlers.py`
- `app/core/security.py`
- `app/core/clients.py`

这些 mypy 问题与本次文档替换和强制重建接口实现无直接关系，本次未在该任务中扩展修复范围。

## 3.5 Review 关注点

- `knowledge_bases.py` 里的既有 `/reindex` 按 Spec 要求未处理，仍保持原样。
- 新增 `/reindex-force` 与既有 `/reindex` 会在功能上相近，但命名上保留参考资料语义，后续如需统一 API，应另开重构任务。
- 替换文档时任务提交失败后保留新 MinIO 对象，这是为了支持后续重新触发强制重建；如果产品希望失败后自动回滚旧文件，需要单独设计回滚策略。
- `reset_for_reindex(...)` 现在会清空旧统计字段，这会改变既有 `/reindex` 触发后的状态展示行为，但符合本阶段 Spec 对“避免旧统计误导前端”的要求。
