from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_app_token_metrics, get_current_user
from app.api.routes.knowledge_bases import _build_index_service, get_permission_service
from app.core.clients import get_minio
from app.core.config import Settings, get_settings
from app.core.context import CurrentUser
from app.core.database import AsyncSessionLocal, get_db
from app.integrations.minio import MinioStorageService
from app.repositories.documents import DocumentRepository
from app.schemas.common import ApiResponse
from app.schemas.knowledge_base import DocumentReindexSubmitResponse
from app.services.document_update import DocumentUpdateService
from app.services.indexing import IndexService
from app.services.permissions import PermissionService
from app.services.token_metrics import TokenUsageRecorder

router = APIRouter()


def get_document_update_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token_metrics: TokenUsageRecorder = Depends(get_app_token_metrics),
) -> DocumentUpdateService:
    """构建文档更新服务及其索引链路依赖。

    Args:
        session: 当前请求注入的异步数据库会话。
        settings: 应用配置，用于 MinIO bucket 和上传大小限制。

    Returns:
        已组装文档仓储、MinIO 和索引服务的 DocumentUpdateService。
    """

    @asynccontextmanager
    async def background_index_service_factory() -> AsyncIterator[IndexService]:
        """为后台重建索引任务创建独立数据库会话和服务实例。"""
        async with AsyncSessionLocal() as background_session:
            background_index_service = _build_index_service(
                background_session,
                settings,
                token_metrics=token_metrics,
                background_service_factory=background_index_service_factory,
                commit_after_status_change=background_session.commit,
                rollback_before_failure_status=background_session.rollback,
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
        token_metrics=token_metrics,
        background_service_factory=background_index_service_factory,
        commit_before_launch=session.commit,
    )
    return DocumentUpdateService(
        document_repository=DocumentRepository(session),
        storage_service=MinioStorageService(client=get_minio(), bucket=settings.minio_bucket),
        index_service=index_service,
        max_upload_file_size_mb=settings.max_upload_file_size_mb,
    )


@router.put(
    "/{kb_id}/documents/{doc_id}/content",
    status_code=status.HTTP_200_OK,
)
async def replace_document_content(
    kb_id: int,
    doc_id: int,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    update_service: DocumentUpdateService = Depends(get_document_update_service),
) -> ApiResponse[DocumentReindexSubmitResponse]:
    """替换已有文档内容，并提交重建索引任务。

    Args:
        kb_id: 文档所属知识库 ID。
        doc_id: 待替换内容的文档 ID。
        file: 新上传的文档原文件。
        user: 当前认证用户上下文。
        permission_service: 权限服务，用于校验写权限。
        update_service: 文档更新服务，用于替换原文件并提交索引任务。

    Returns:
        包含文档 ID、文件名、状态和任务 ID 的统一响应。
    """
    # 替换原文会改写可检索内容，必须先做写权限硬校验。
    await permission_service.require_write(kb_id, user)
    return ApiResponse.ok(await update_service.replace_content(kb_id, doc_id, file, user))


@router.post(
    "/{kb_id}/documents/{doc_id}/reindex-force",
    status_code=status.HTTP_200_OK,
)
async def force_reindex_document(
    kb_id: int,
    doc_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    update_service: DocumentUpdateService = Depends(get_document_update_service),
) -> ApiResponse[DocumentReindexSubmitResponse]:
    """强制重建已有文档索引，不替换原始文件。

    Args:
        kb_id: 文档所属知识库 ID。
        doc_id: 待强制重建索引的文档 ID。
        user: 当前认证用户上下文。
        permission_service: 权限服务，用于校验写权限。
        update_service: 文档更新服务，用于重置状态并提交重建任务。

    Returns:
        包含文档 ID、文件名、状态和任务 ID 的统一响应。
    """
    # 强制重建会产生新版本 chunk，也必须要求写权限。
    await permission_service.require_write(kb_id, user)
    return ApiResponse.ok(await update_service.force_reindex(kb_id, doc_id))
