# 知识库管理接口——上传文档与索引状态 Spec

## 2.1 任务背景

基于 [06-知识库管理接口——上传文档与索引状态.md](/D:/Code/python/Practical_Project/rag-kb/docs/plans/06-知识库管理接口——上传文档与索引状态.md)，本阶段要把已经完成的离线索引能力封装为可调用的 FastAPI 管理接口，打通以下链路：

```plain
前端上传文件
  -> 原始文件保存到 MinIO
  -> 创建 kb_document
  -> 提交索引任务
  -> 前端轮询索引状态
  -> 下载 / 删除 / 重建索引等管理动作
```

本阶段的核心不是继续扩展 `IndexService` 的内部索引逻辑，而是新增“接口层 + 文档管理服务 + 权限校验 + MinIO 完整接入”，让现有索引管道真正进入业务流。

前置条件：

- `IndexService`、`DocumentRepository`、`IndexTaskRepository`、`ChunkRepository` 已存在。
- `KbDocument`、`IndexTask`、`KnowledgeBase`、`KbPermission` 等模型已存在于 [kb.py](/D:/Code/python/Practical_Project/rag-kb/app/models/kb.py)。
- `app/core/clients.py` 已初始化同步版 MinIO SDK 客户端。

## Context7 查询结论

本 spec 编写前使用 Context7 查询了 FastAPI 关于 `UploadFile`、`File(...)`、`APIRouter` 依赖、`BackgroundTasks` 和 `StreamingResponse` 的最新用法。

查询命令：

```powershell
npx.cmd ctx7@latest library fastapi "UploadFile File Depends APIRouter BackgroundTasks response_model form multipart async endpoints"
npx.cmd ctx7@latest docs /fastapi/fastapi "UploadFile with File in APIRouter, Depends dependency injection, BackgroundTasks for tasks after response, note for heavy background work, response_model and status_code for async endpoints"
npx.cmd ctx7@latest docs /fastapi/fastapi "StreamingResponse return bytes iterator file download headers filename content-disposition UploadFile async endpoint"
```

结论：

- FastAPI 文件上传推荐使用 `UploadFile` 配合 `File(...)`。
- `APIRouter` 可设置统一 `prefix`、`tags` 和 `dependencies`，适合知识库管理接口分组。
- `BackgroundTasks` 适合响应后执行附加任务，但不适合再包一层重型索引调度。
- `UploadFile` 的异步读写底层会进入线程池，适合处理上传文件 I/O。
- 文件下载可使用 `StreamingResponse` 返回内存或生成器形式的二进制流，并设置 `Content-Disposition`。

因此，本阶段采用以下取舍：

- 上传接口本身不再额外依赖 `BackgroundTasks` 调度索引。
- 路由层只调用 `KnowledgeBaseService.upload_document(...)`，再由服务层调用既有 `IndexService.submit_index_task(...)`。
- 原始文件下载使用 `StreamingResponse(BytesIO(...))` 返回。

## 2.2 范围对齐（Scope Alignment）

本次技术交付物必须与 Plan 的 In Scope 对齐，避免编码时越界。

### In Scope

- 新增知识库管理路由层：
  - 查询当前用户可访问知识库
  - 创建知识库
  - 上传文档
  - 查询文档索引状态
  - 查询知识库文档列表
  - 下载原始文件
  - 删除文档
  - 手动触发重建索引
- 新增 `KnowledgeBaseService`，负责编排知识库、文档记录、索引任务提交。
- 新增 `PermissionService`，负责读/写权限判断。
- 将当前占位版 `MinioStorageService` 扩展为完整服务，至少支持 `upload` / `download` / `delete`。
- 定义本阶段所需的 Pydantic request/response schema。
- 衔接现有 `IndexService`，让上传接口能真正触发后台索引。

### Out of Scope

- 不实现完整认证体系接入；当前用户上下文可先用占位依赖或 `UserContext` 方案承接。
- 不实现知识库授权管理接口。
- 不实现前端页面与 UI 轮询逻辑。
- 不扩展在线问答、检索、Reranker、回答生成链路。
- 不实现预签名 URL、分片上传、断点续传。

## 建议文件结构

建议新增或修改以下文件：

```plain
app/api/routes/knowledge_bases.py
app/schemas/common.py
app/schemas/knowledge_base.py
app/services/knowledge_base.py
app/services/permissions.py
app/integrations/minio.py
app/repositories/knowledge_bases.py
app/repositories/permissions.py
tests/services/test_knowledge_base_service.py
tests/services/test_permission_service.py
tests/api/test_knowledge_bases.py
```

职责建议：

- `app/api/routes/knowledge_bases.py`
  - 暴露 HTTP 接口，做参数接收、状态码声明、权限入口调用。
- `app/schemas/common.py`
  - 通用响应包装，如 `ApiResponse[T]`。
- `app/schemas/knowledge_base.py`
  - 本阶段所有知识库与文档管理 schema。
- `app/services/knowledge_base.py`
  - 知识库创建、文档上传、删除、重建索引、文档列表查询。
- `app/services/permissions.py`
  - 读写权限校验。
- `app/core/context.py` 与 `app/api/dependencies.py`
  - 复用现有 `CurrentUser` 与 `get_current_user()` 占位依赖，后续可替换为 JWT / Session 注入。
- `app/integrations/minio.py`
  - 从最小下载接口升级为完整 MinIO 业务服务。
- `app/repositories/knowledge_bases.py`
  - 知识库读写仓储。
- `app/repositories/permissions.py`
  - 权限读写仓储。

## 2.3 数据输入/输出模型（I/O Models）

### 路由分组

统一前缀建议：

```plain
/api/v1/kb
```

对应现有配置：

```python
settings.api_v1_prefix == "/api/v1"
```

路由建议挂载方式：

```python
api_router.include_router(
    knowledge_bases.router,
    prefix="/kb",
    tags=["knowledge-bases"],
)
```

### 当前用户上下文

本阶段不直接接完整认证系统，先定义一个最小上下文模型：

```python
@dataclass(frozen=True)
class CurrentUser:
    user_id: int
    department_id: str
    role: str
```

建议占位依赖：

```python
async def get_current_user() -> AsyncIterator[CurrentUser]:
    user = CurrentUser(user_id=1, department_id="default", role="ADMIN")
    token_var = current_user_var.set(user)
    try:
        yield user
    finally:
        current_user_var.reset(token_var)
```

说明：

- 这是临时接入层，不应把它与业务逻辑强耦合。
- `PermissionService` 和 `KnowledgeBaseService` 接收 `CurrentUser`，而不是直接从全局变量读取。

### 输入模型（Input）

#### 1. 创建知识库

请求：

```python
class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    department_id: str = Field(min_length=1, max_length=50)
    is_public: bool = False
```

接口：

```plain
POST /api/v1/kb
```

#### 2. 上传文档

请求参数：

```python
kb_id: int  # path
file: UploadFile = File(...)
```

校验规则：

- 文件名不能为空。
- 文件扩展名只允许：`.pdf`、`.docx`、`.md`、`.txt`
- 文件大小不得超过 `settings.max_upload_file_size_mb`。
- 上传目标知识库必须存在，且当前用户具备写权限。

接口：

```plain
POST /api/v1/kb/{kb_id}/documents
```

#### 3. 查询索引状态

请求参数：

```python
kb_id: int  # path
doc_id: int  # path
```

接口：

```plain
GET /api/v1/kb/{kb_id}/documents/{doc_id}/status
```

#### 4. 查询文档列表

请求参数：

```python
kb_id: int  # path
```

接口：

```plain
GET /api/v1/kb/{kb_id}/documents
```

#### 5. 下载原始文件

请求参数：

```python
kb_id: int  # path
doc_id: int  # path
```

接口：

```plain
GET /api/v1/kb/{kb_id}/documents/{doc_id}/download
```

#### 6. 删除文档

请求参数：

```python
kb_id: int  # path
doc_id: int  # path
```

接口：

```plain
DELETE /api/v1/kb/{kb_id}/documents/{doc_id}
```

#### 7. 重建索引

请求参数：

```python
kb_id: int  # path
doc_id: int  # path
```

接口：

```plain
POST /api/v1/kb/{kb_id}/documents/{doc_id}/reindex
```

### 输出模型（Output）

#### 统一响应包装

建议保留统一信封结构：

```python
class ApiResponse(GenericModel, Generic[T]):
    code: int
    message: str
    data: T | None
```

建议约定：

- 成功：`code=200`, `message="success"`
- 业务失败：抛出 `HTTPException`，由异常处理中间层转成统一错误响应

#### 知识库列表项

```python
class KnowledgeBaseItem(BaseModel):
    id: int
    name: str
    description: str | None
    department_id: str
    is_public: bool
    created_by: int
    created_at: datetime
    permission: str
```

#### 上传文档响应

上传是异步索引，建议返回 `202 Accepted`：

```python
class DocumentUploadResponse(BaseModel):
    doc_id: int
    file_name: str
    status: str  # PENDING
    message: str
```

#### 文档列表项响应

文档列表接口的 HTTP 响应边界必须返回 Pydantic DTO，不直接暴露 SQLAlchemy ORM 对象：

```python
class DocumentItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int
    file_name: str
    file_type: str
    file_size: int
    status: str
    error_msg: str | None
    chunk_count: int | None
    token_count: int | None
    version: int
    uploaded_by: int
    uploaded_at: datetime | None
    indexed_at: datetime | None
```

说明：

- `KnowledgeBaseService.list_documents(...)` 可以继续返回 `list[KbDocument]`，保持服务层贴近仓储模型。
- 路由层负责使用 `DocumentItem.model_validate(document)` 将 ORM 对象转换为响应 DTO。
- `DocumentItem` 必须开启 `from_attributes=True`，避免 `ApiResponse[list[...]]` 包装原始 ORM 对象时触发 Pydantic 序列化错误。

#### 索引状态响应

```python
class IndexStatusResponse(BaseModel):
    doc_id: int
    file_name: str
    status: str
    error_msg: str | None
    chunk_count: int | None
    token_count: int | None
    indexed_at: datetime | None
    retry_count: int
```

### 状态码建议

| 场景 | 状态码 |
| --- | --- |
| 查询列表 / 查询状态 / 下载成功 | `200 OK` |
| 创建知识库 | `201 Created` |
| 上传文档并提交索引任务 | `202 Accepted` |
| 重建索引任务已提交 | `202 Accepted` |
| 删除成功 | `200 OK` |
| 参数非法 | `400 Bad Request` |
| 未认证 | `401 Unauthorized` |
| 无权限 | `403 Forbidden` |
| 资源不存在 | `404 Not Found` |

## 2.4 代码执行流程（Execution Flow）

### 总体调用链

```plain
FastAPI APIRouter
  -> get_current_user() 依赖注入
  -> PermissionService / KnowledgeBaseService
  -> Repository / MinioStorageService / IndexService
  -> SQLAlchemy / MinIO / 后台索引任务
```

### 1. 查询当前可访问知识库

```plain
GET /api/v1/kb
  -> 注入 CurrentUser
  -> KnowledgeBaseService.list_accessible(user)
  -> KnowledgeBaseRepository 查询公开库、用户授权、部门授权
  -> PermissionService 计算每个知识库的最高权限级别
  -> 组装 KnowledgeBaseItem 列表
  -> 返回 ApiResponse[list[KnowledgeBaseItem]]
```

### 2. 创建知识库

```plain
POST /api/v1/kb
  -> 注入 CurrentUser
  -> 校验 request body
  -> KnowledgeBaseService.create(request, user)
  -> 写入 kb_knowledge_base
  -> 同时写入一条创建者 ADMIN 权限
  -> 返回 ApiResponse[KnowledgeBaseItem]
```

### 3. 上传文档

```plain
POST /api/v1/kb/{kb_id}/documents
  -> 注入 CurrentUser
  -> PermissionService.require_write(kb_id, user)
  -> KnowledgeBaseService.upload_document(kb_id, file, user)
       -> validate_file_type(file.filename)
       -> validate_file_size(file.size / 手动统计)
       -> MinioStorageService.upload(kb_id, file)
       -> DocumentRepository.create(...)
       -> IndexService.submit_index_task(doc_id)
  -> 返回 202 + DocumentUploadResponse(status=PENDING)
```

关键点：

- 上传接口只提交任务，不等待索引完成。
- 不在路由层使用 `BackgroundTasks` 再包一层任务，避免和 `IndexService` 的内部后台调度重复。
- `IndexService.submit_index_task(...)` 已具备本地后台投递能力，因此路由层只需正常 `await` 服务调用并快速返回。
- 上传触发的是首次索引 `INDEX`，文档初始版本为 `1`，索引完成后仍保持 `version=1`。

### 4. 查询索引状态

```plain
GET /api/v1/kb/{kb_id}/documents/{doc_id}/status
  -> 注入 CurrentUser
  -> PermissionService.require_read(kb_id, user)
  -> KnowledgeBaseService.get_index_status(kb_id, doc_id, user)
       -> 查询 kb_document
       -> 校验文档属于当前 kb_id
       -> 查询最新 kb_index_task
       -> 组装 IndexStatusResponse
  -> 返回 ApiResponse[IndexStatusResponse]
```

### 5. 查询文档列表

```plain
GET /api/v1/kb/{kb_id}/documents
  -> 注入 CurrentUser
  -> PermissionService.require_read(kb_id, user)
  -> KnowledgeBaseService.list_documents(kb_id, user)
       -> 查询 kb_document where kb_id=? and is_deleted=false
  -> 路由层转换为 list[DocumentItem]
  -> 返回 ApiResponse[list[DocumentItem]]
```

关键点：

- 服务层可以使用 `KbDocument` 承接数据库记录，但接口响应必须以 `DocumentItem` 作为稳定契约。
- 不允许把原始 SQLAlchemy ORM 对象直接塞入 `ApiResponse`，否则在泛型响应序列化时会出现 unknown type 错误。

### 6. 下载原始文件

```plain
GET /api/v1/kb/{kb_id}/documents/{doc_id}/download
  -> 注入 CurrentUser
  -> PermissionService.require_read(kb_id, user)
  -> KnowledgeBaseService.download_document(kb_id, doc_id, user)
       -> 查询 kb_document
       -> MinioStorageService.download(minio_path)
       -> 返回 file_name + bytes
  -> 路由层使用 StreamingResponse(BytesIO(content))
  -> 设置 Content-Disposition 附件文件名
```

### 7. 删除文档

```plain
DELETE /api/v1/kb/{kb_id}/documents/{doc_id}
  -> 注入 CurrentUser
  -> PermissionService.require_write(kb_id, user)
  -> KnowledgeBaseService.delete_document(kb_id, doc_id, user)
       -> 查询 kb_document
       -> 软删除文档记录
       -> 硬删除 chunk 数据
       -> 删除 MinIO 原始文件
  -> 返回 ApiResponse[None]
```

### 8. 重建索引

```plain
POST /api/v1/kb/{kb_id}/documents/{doc_id}/reindex
  -> 注入 CurrentUser
  -> PermissionService.require_write(kb_id, user)
  -> KnowledgeBaseService.reindex_document(kb_id, doc_id, user)
       -> 查询 kb_document
       -> DONE 文档保持发布状态；非 DONE 文档重置为 PENDING / 清空 error_msg
       -> 调用 IndexService.reindex_document(doc_id)
       -> 索引管道按 REINDEX 将 document.version 递增
  -> 返回 202 + ApiResponse[str]
```

## 模块边界设计

### APIRouter

职责：

- 接收 HTTP 入参。
- 注入 `CurrentUser`。
- 调用权限校验。
- 返回 schema 或 `StreamingResponse`。

不负责：

- 直接操作数据库。
- 直接写 MinIO。
- 直接更新索引任务状态。

### KnowledgeBaseService

职责：

- 创建知识库。
- 上传文档并创建文档记录。
- 查询文档列表与索引状态。
- 删除文档。
- 触发重建索引。

不负责：

- 解析文档与向量化。
- 计算检索结果。
- 实现认证中间件。

### PermissionService

职责：

- `require_read(kb_id, user)`
- `require_write(kb_id, user)`
- 计算知识库权限级别展示值

权限来源：

- 管理员角色
- 用户显式授权
- 部门显式授权
- 知识库公开标记

### MinioStorageService

建议接口：

```python
class MinioStorageService:
    async def upload(self, kb_id: int, file: UploadFile) -> str:
        ...

    async def download(self, object_key: str) -> bytes:
        ...

    async def delete(self, object_key: str) -> None:
        ...
```

对象路径建议：

```plain
kb/{kb_id}/{short_uuid}-{original_file_name}
```

重要约束：

- 当前项目中的 MinIO SDK 客户端是同步版。
- 在异步服务中调用上传、下载、删除时，应通过 `asyncio.to_thread(...)` 包装同步 SDK，避免阻塞事件循环。
- Bucket 检查与创建可在服务内部惰性执行，或在启动阶段统一初始化；本阶段先允许服务内部保证 bucket 存在。

### Repository

建议新增：

- `KnowledgeBaseRepository`
- `KbPermissionRepository`

现有可复用：

- `DocumentRepository`
- `IndexTaskRepository`
- `ChunkRepository`

仓储层职责：

- 封装 SQLAlchemy 查询与写入。
- 屏蔽服务层对 ORM 细节的直接依赖。
- 不在仓储层做权限判断。

## 2.5 技术约束与最佳实践

### 1. FastAPI 约束

- 上传接口使用 `UploadFile = File(...)`，不要用 `bytes` 一次性吃完整文件。
- 文件下载优先使用 `StreamingResponse` 返回，避免把下载逻辑写成字符串或 JSON 包装。
- 路由层使用 `response_model` 或函数返回类型声明 schema，保证 OpenAPI 文档清晰。
- 路由层不承载复杂业务判断，复杂逻辑下沉到 service。

### 2. 后台任务策略

- 本阶段不在上传接口里使用 `BackgroundTasks` 触发索引。
- 原因是 `IndexService.submit_index_task(...)` 已经自行创建后台任务并维护重试调度。
- 如果路由层再包一层 `BackgroundTasks`，会形成“双重调度”，增加排错难度。
- 请求级索引服务启动后台任务前必须提交当前事务，确保新建文档、重建状态和索引任务对后台独立会话可见。
- 后台任务必须通过独立 `AsyncSessionLocal()` 构建仓储和 `IndexService`，不能复用 FastAPI 请求依赖注入的 `AsyncSession`。
- 后台任务写入 `task(RUNNING)`、`document(PROCESSING)` 后必须立即提交独立会话，确保文档加载、分块、Embedding 阶段的轮询结果不是长期停留在 `PENDING`。

### 3. MinIO 与事件循环

- `minio` Python SDK 是同步客户端，不能在 async 路径中直接长时间阻塞调用。
- 上传、下载、删除都应用 `asyncio.to_thread(...)` 或等价线程池包装。
- `UploadFile` 的读取是异步友好的，但把文件内容真正写入 MinIO 时仍需考虑 SDK 的同步特性。

### 4. 事务边界

- 创建知识库与创建创建者默认权限，应放在同一事务中。
- 文档上传阶段遵循：
  - 先上传 MinIO
  - 再写 `kb_document`
  - 再提交索引任务
- 提交索引任务后、后台任务启动前，需要提交当前事务，避免后台任务读取不到任务记录或复用请求 session。
- 后台任务启动并写入执行态后，需要再次提交执行态状态，避免长耗时索引阶段对外不可观测。
- 如果 MinIO 上传成功但数据库写入失败，需要明确补偿策略：
  - 本阶段先记录错误并尝试删除刚上传的 MinIO 对象。
- 删除文档阶段遵循：
  - 先软删除文档记录
  - 再删 chunk
  - 再删 MinIO 对象
- 删除 MinIO 失败不应回滚整个删除动作，但必须记录告警日志。

### 5. 权限与数据一致性

- 所有文档相关接口都必须先校验知识库读/写权限。
- 查询文档状态、下载、删除、重建时必须校验 `doc_id` 属于 `kb_id`，不能只校验 `doc_id` 是否存在。
- 公开知识库只放开读权限，不自动放开写权限。

### 6. 异常处理规范

- 参数错误：抛 `HTTPException(status_code=400, ...)`
- 无权限：抛 `HTTPException(status_code=403, ...)`
- 资源不存在：抛 `HTTPException(status_code=404, ...)`
- MinIO 上传/下载异常：记录结构化日志，并向上抛业务异常
- 索引任务提交失败：需要标记文档错误状态，避免出现“文档记录存在但没有任务”的悬挂记录

### 7. 日志记录点

至少记录以下日志点：

- 知识库创建成功
- 文档上传成功：`kb_id`、`doc_id`、`file_name`、`minio_path`
- 文档上传失败：阶段、异常、是否已补偿删除 MinIO 对象
- 文档删除成功/失败
- 文档重建索引触发成功
- 权限拒绝事件：`user_id`、`kb_id`、action

### 8. 文件类型与大小校验

支持文件类型：

```plain
PDF / DOCX / MD / TXT
```

大小限制：

- 单文件大小使用 `settings.max_upload_file_size_mb`
- 如后续需要网关级或 ASGI 级请求体限制，再在部署层追加

### 9. 测试建议

优先覆盖：

- 创建知识库自动授予创建者 `ADMIN`
- 管理员、用户授权、部门授权、公开知识库的读权限判断
- `WRITE / ADMIN` 才允许上传、删除、重建
- 上传成功后：
  - MinIO `upload(...)` 被调用
  - `kb_document` 被创建
  - `IndexService.submit_index_task(...)` 被调用
- 上传失败补偿删除 MinIO
- 状态接口能正确返回文档状态和最近任务的 `retry_count`
- 下载接口返回附件响应头
- 删除文档执行“软删文档 + 硬删 chunk + 删 MinIO”
- 重建索引调用 `IndexService.reindex_document(...)`

## 2.6 执行标准

### Git Commit 规范

遵循 Conventional Commits：

- `feat: add knowledge base management routes`
- `feat: implement minio upload/download/delete service`
- `feat: add permission service for kb access control`
- `test: add api tests for kb document management`
- `docs: add kb management spec`

### 代码风格要求

- 保持 Python 3.12 类型标注完整。
- 路由、service、repository、schema 分层清晰，避免把整个流程塞进单个 endpoint。
- 代码不要切得太碎；辅助方法只有在确实降低主流程阅读成本时才抽出。
- 方法顺序尽量贴合执行流程，便于 review。
- 中文注释只写在关键流程、状态流转、异常边界和不直观决策点。
- 不直接把 ORM 模型原样返回给前端，统一经 schema 输出。

## 验收标准

### 场景一：上传文档

GIVEN 当前用户对知识库有写权限

WHEN 调用上传接口

THEN 原始文件进入 MinIO，文档记录写入数据库，索引任务被提交，接口返回 `202` 和 `PENDING`。

### 场景二：查询索引状态

GIVEN 文档已存在并已提交索引任务

WHEN 调用状态接口

THEN 返回文档状态、错误信息、chunk 数、token 数、索引完成时间和最新任务重试次数。

### 场景三：权限拒绝

GIVEN 当前用户没有目标知识库写权限

WHEN 调用上传、删除或重建索引接口

THEN 返回 `403 Forbidden`，不进入后续业务服务。

### 场景四：删除文档

GIVEN 文档存在且用户有写权限

WHEN 调用删除接口

THEN 文档软删除、chunk 硬删除、MinIO 对象删除，并返回成功消息。

### 场景五：重建索引

GIVEN 文档存在且用户有写权限

WHEN 调用重建索引接口

THEN 重新提交索引任务，接口返回 `202`；若文档已发布，主文档状态保持 `DONE`。

## 参考资料

- [06-知识库管理接口——上传文档与索引状态.md](/D:/Code/python/Practical_Project/rag-kb/docs/plans/06-知识库管理接口——上传文档与索引状态.md)
- [10-知识库管理接口——上传文档与索引状态.md](/D:/Code/python/Practical_Project/rag-kb/docs/references/10-知识库管理接口——上传文档与索引状态.md)
- FastAPI Context7 docs: `/fastapi/fastapi`
