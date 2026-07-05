from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.clients import get_embeddings, get_minio, get_redis
from app.core.config import Settings, get_settings
from app.core.context import CurrentUser
from app.core.database import AsyncSessionLocal, get_db
from app.integrations.minio import MinioStorageService
from app.repositories.chunks import ChunkRepository
from app.repositories.documents import DocumentRepository
from app.repositories.index_tasks import IndexTaskRepository
from app.repositories.knowledge_bases import KnowledgeBaseRepository
from app.repositories.permissions import KbPermissionRepository
from app.schemas.common import ApiResponse
from app.schemas.knowledge_base import (
    DocumentItem,
    DocumentUploadResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseItem,
)
from app.services.chunking import ChunkService
from app.services.document_loader import DocumentLoaderService, MarkdownParser, PdfParser, TxtParser, WordParser
from app.services.embedding import EmbeddingService
from app.services.indexing import IndexService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.permissions import PermissionService

router = APIRouter()


def _build_index_service(
    session: AsyncSession,
    settings: Settings,
    *,
    background_service_factory=None,
    commit_before_launch=None,
    commit_after_status_change=None,
) -> IndexService:
    """构建索引服务及其底层依赖。

    Args:
        session: 索引服务当前使用的数据库会话。
        settings: 应用配置，用于 MinIO bucket 和文档解析器。
        background_service_factory: 后台任务独立服务工厂。
        commit_before_launch: 后台任务启动前的事务提交钩子。
        commit_after_status_change: 任务进入执行态后的事务提交钩子。

    Returns:
        已组装仓储、存储、加载、分块和向量化能力的 IndexService。
    """
    # 索引后台任务需要复用同一套依赖组装逻辑，但数据库会话必须由调用方显式传入。
    return IndexService(
        document_repository=DocumentRepository(session),
        task_repository=IndexTaskRepository(session),
        chunk_repository=ChunkRepository(session),
        storage_service=MinioStorageService(client=get_minio(), bucket=settings.minio_bucket),
        loader_service=DocumentLoaderService(
            [
                TxtParser(),
                MarkdownParser(),
                PdfParser(settings=settings),
                WordParser(settings=settings),
            ]
        ),
        chunk_service=ChunkService(),
        embedding_service=EmbeddingService(get_embeddings(), get_redis()),
        background_service_factory=background_service_factory,
        commit_before_launch=commit_before_launch,
        commit_after_status_change=commit_after_status_change,
    )


def get_permission_service(
    session: AsyncSession = Depends(get_db),
) -> PermissionService:
    """构建知识库权限服务。

    Args:
        session: 当前请求注入的异步数据库会话。

    Returns:
        可用于读写权限校验和权限查询的 PermissionService。
    """
    return PermissionService(
        knowledge_base_repository=KnowledgeBaseRepository(session),
        permission_repository=KbPermissionRepository(session),
    )


def get_knowledge_base_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> KnowledgeBaseService:
    """构建知识库业务服务及其索引链路依赖。

    Args:
        session: 当前请求注入的异步数据库会话。
        settings: 应用配置，用于文件桶、上传限制和文档解析器配置。

    Returns:
        已组装仓储、MinIO、文档加载、分块、Embedding 和索引服务的 KnowledgeBaseService。
    """
    document_repository = DocumentRepository(session)
    task_repository = IndexTaskRepository(session)
    chunk_repository = ChunkRepository(session)
    storage_service = MinioStorageService(client=get_minio(), bucket=settings.minio_bucket)

    @asynccontextmanager
    async def background_index_service_factory() -> AsyncIterator[IndexService]:
        """为后台索引任务创建独立数据库会话和服务实例。"""
        async with AsyncSessionLocal() as background_session:
            background_index_service = _build_index_service(
                background_session,
                settings,
                background_service_factory=background_index_service_factory,
                commit_after_status_change=background_session.commit,
            )
            try:
                yield background_index_service
                await background_session.commit()
            except Exception:
                await background_session.rollback()
                raise

    index_service = _build_index_service(
        session,
        settings,
        background_service_factory=background_index_service_factory,
        commit_before_launch=session.commit,
    )
    # KnowledgeBaseService 只暴露业务入口，索引管道细节收敛在 IndexService 内部。
    return KnowledgeBaseService(
        knowledge_base_repository=KnowledgeBaseRepository(session),
        permission_repository=KbPermissionRepository(session),
        document_repository=document_repository,
        task_repository=task_repository,
        chunk_repository=chunk_repository,
        storage_service=storage_service,
        index_service=index_service,
        max_upload_file_size_mb=settings.max_upload_file_size_mb,
    )


@router.get("")
async def list_knowledge_bases(
    user: CurrentUser = Depends(get_current_user),
    kb_service: KnowledgeBaseService = Depends(get_knowledge_base_service),
    permission_service: PermissionService = Depends(get_permission_service),
) -> ApiResponse[list[KnowledgeBaseItem]]:
    """查询当前用户可访问的知识库列表。

    Args:
        user: 当前认证用户上下文。
        kb_service: 知识库业务服务，用于获取用户可见知识库。
        permission_service: 权限服务，用于补充用户在每个知识库上的最高权限。

    Returns:
        包含知识库基础信息和当前用户权限级别的统一响应。
    """
    knowledge_bases = await kb_service.list_accessible(user)
    items: list[KnowledgeBaseItem] = []
    for knowledge_base in knowledge_bases:
        # 管理员天然拥有管理权限；普通用户需要从权限关系中计算最高权限。
        permission = "ADMIN" if user.is_admin else await permission_service.get_highest_permission(
            knowledge_base.id,
            user,
        )
        # 公开知识库允许读取，即使没有显式授权记录也要返回 READ。
        if permission is None and knowledge_base.is_public:
            permission = "READ"
        items.append(
            KnowledgeBaseItem(
                id=knowledge_base.id,
                name=knowledge_base.name,
                description=knowledge_base.description,
                department_id=knowledge_base.department_id,
                is_public=knowledge_base.is_public,
                created_by=knowledge_base.created_by,
                created_at=knowledge_base.created_at,
                permission=permission or "READ",
            )
        )
    return ApiResponse.ok(items)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    user: CurrentUser = Depends(get_current_user),
    kb_service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> ApiResponse[KnowledgeBaseItem]:
    """创建知识库并返回创建者视角的知识库信息。

    Args:
        request: 创建知识库的请求体。
        user: 当前认证用户上下文。
        kb_service: 知识库业务服务，用于创建知识库和写入默认权限。

    Returns:
        包含新知识库信息的统一响应，创建者权限固定为 ADMIN。
    """
    knowledge_base = await kb_service.create(request, user)
    return ApiResponse.ok(
        KnowledgeBaseItem(
            id=knowledge_base.id,
            name=knowledge_base.name,
            description=knowledge_base.description,
            department_id=knowledge_base.department_id,
            is_public=knowledge_base.is_public,
            created_by=knowledge_base.created_by,
            created_at=knowledge_base.created_at,
            permission="ADMIN",
        )
    )


@router.post("/{kb_id}/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    kb_id: int,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    kb_service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> ApiResponse[DocumentUploadResponse]:
    """上传文档并提交异步索引任务。

    Args:
        kb_id: 目标知识库 ID。
        file: FastAPI 接收的上传文件对象。
        user: 当前认证用户上下文。
        permission_service: 权限服务，用于校验当前用户是否可写该知识库。
        kb_service: 知识库业务服务，用于保存文件、创建文档记录并提交索引任务。

    Returns:
        包含文档 ID、文件名和初始索引状态的统一响应。
    """
    # 写权限必须在上传和索引提交前硬校验，避免无权限文件进入存储或索引链路。
    await permission_service.require_write(kb_id, user)
    document = await kb_service.upload_document(kb_id, file, user)
    return ApiResponse.ok(DocumentUploadResponse.submitted(document.id, document.file_name))


@router.get("/{kb_id}/documents/{doc_id}/status")
async def get_index_status(
    kb_id: int,
    doc_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    kb_service: KnowledgeBaseService = Depends(get_knowledge_base_service),
):
    """查询文档索引任务的最新状态。

    Args:
        kb_id: 文档所属知识库 ID。
        doc_id: 待查询状态的文档 ID。
        user: 当前认证用户上下文。
        permission_service: 权限服务，用于校验读取权限。
        kb_service: 知识库业务服务，用于聚合文档状态和最新任务状态。

    Returns:
        包含文档索引状态、统计信息和重试次数的统一响应。
    """
    # 状态查询也需要读权限，避免通过 doc_id 枚举其他知识库文档。
    await permission_service.require_read(kb_id, user)
    return ApiResponse.ok(await kb_service.get_index_status(kb_id, doc_id))


@router.get("/{kb_id}/documents")
async def list_documents(
    kb_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    kb_service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> ApiResponse[list[DocumentItem]]:
    """查询知识库下的文档列表。

    Args:
        kb_id: 目标知识库 ID。
        user: 当前认证用户上下文。
        permission_service: 权限服务，用于校验读取权限。
        kb_service: 知识库业务服务，用于读取文档列表。

    Returns:
        包含文档列表的统一响应。
    """
    # 文档列表属于知识库数据面，必须在服务查询前完成权限硬过滤。
    await permission_service.require_read(kb_id, user)
    documents = await kb_service.list_documents(kb_id)
    # SQLAlchemy ORM 对象不能直接进入 ApiResponse；先转换为稳定的响应 DTO。
    return ApiResponse.ok([DocumentItem.model_validate(document) for document in documents])


@router.get("/{kb_id}/documents/{doc_id}/download")
async def download_document(
    kb_id: int,
    doc_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    kb_service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> StreamingResponse:
    """下载知识库中的原始文档文件。

    Args:
        kb_id: 文档所属知识库 ID。
        doc_id: 待下载的文档 ID。
        user: 当前认证用户上下文。
        permission_service: 权限服务，用于校验读取权限。
        kb_service: 知识库业务服务，用于读取文件名和文件内容。

    Returns:
        带 Content-Disposition 附件头的流式文件响应。
    """
    await permission_service.require_read(kb_id, user)
    file_name, content = await kb_service.download_document(kb_id, doc_id)
    # 文件名需要按 RFC 5987 编码，避免中文或空格在下载响应头中乱码。
    encoded_name = quote(file_name)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@router.delete("/{kb_id}/documents/{doc_id}")
async def delete_document(
    kb_id: int,
    doc_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    kb_service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> ApiResponse[None]:
    """删除知识库文档及其索引数据。

    Args:
        kb_id: 文档所属知识库 ID。
        doc_id: 待删除的文档 ID。
        user: 当前认证用户上下文。
        permission_service: 权限服务，用于校验写权限。
        kb_service: 知识库业务服务，用于删除文档、chunk 和对象存储文件。

    Returns:
        data 为 None 的统一成功响应。
    """
    # 删除会影响原文和检索结果，必须使用写权限而不是读权限。
    await permission_service.require_write(kb_id, user)
    await kb_service.delete_document(kb_id, doc_id)
    return ApiResponse.ok(None)


@router.post("/{kb_id}/documents/{doc_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_document(
    kb_id: int,
    doc_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    kb_service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> ApiResponse[str]:
    """重新提交文档索引任务。

    Args:
        kb_id: 文档所属知识库 ID。
        doc_id: 待重建索引的文档 ID。
        user: 当前认证用户上下文。
        permission_service: 权限服务，用于校验写权限。
        kb_service: 知识库业务服务，用于重置文档状态并提交重建任务。

    Returns:
        表示重建索引任务已提交的统一响应。
    """
    # 重建索引会改写 chunk 和任务状态，必须要求写权限。
    await permission_service.require_write(kb_id, user)
    await kb_service.reindex_document(kb_id, doc_id)
    return ApiResponse.ok("重建索引任务已提交，请通过 status 接口查询进度")
