from __future__ import annotations

from fastapi import HTTPException, UploadFile, status

from app.core.context import CurrentUser
from app.integrations.minio import MinioStorageService
from app.models import KbDocument, KnowledgeBase, PermissionSubjectType
from app.repositories.chunks import ChunkRepository
from app.repositories.documents import DocumentRepository
from app.repositories.index_tasks import IndexTaskRepository
from app.repositories.knowledge_bases import KnowledgeBaseRepository
from app.repositories.permissions import KbPermissionRepository
from app.schemas.knowledge_base import IndexStatusResponse, KnowledgeBaseCreateRequest
from app.services.indexing import IndexService


class KnowledgeBaseService:
    """知识库管理服务，串联文档上传、记录创建、索引提交和文档管理动作。"""

    _supported_extensions = {
        ".pdf": "PDF",
        ".docx": "DOCX",
        ".md": "MD",
        ".txt": "TXT",
    }

    def __init__(
        self,
        *,
        knowledge_base_repository: KnowledgeBaseRepository,
        permission_repository: KbPermissionRepository,
        document_repository: DocumentRepository,
        task_repository: IndexTaskRepository,
        chunk_repository: ChunkRepository,
        storage_service: MinioStorageService,
        index_service: IndexService,
        max_upload_file_size_mb: int,
    ) -> None:
        self.knowledge_base_repository = knowledge_base_repository
        self.permission_repository = permission_repository
        self.document_repository = document_repository
        self.task_repository = task_repository
        self.chunk_repository = chunk_repository
        self.storage_service = storage_service
        self.index_service = index_service
        self.max_upload_file_size_bytes = max_upload_file_size_mb * 1024 * 1024

    async def create(
        self,
        request: KnowledgeBaseCreateRequest,
        user: CurrentUser,
    ) -> KnowledgeBase:
        """创建知识库，并给创建者授予 ADMIN 权限。"""
        knowledge_base = await self.knowledge_base_repository.create(
            name=request.name,
            description=request.description,
            department_id=request.department_id,
            is_public=request.is_public,
            created_by=user.user_id,
        )
        await self.permission_repository.create_admin_permission(knowledge_base.id, user.user_id)
        return knowledge_base

    async def list_accessible(self, user: CurrentUser) -> list[KnowledgeBase]:
        """查询当前用户可访问的知识库列表。"""
        if user.is_admin:
            return await self.knowledge_base_repository.list_not_deleted()

        permissions_by_kb: dict[int, str] = {}
        for permission in await self.permission_repository.list_by_subject(
            PermissionSubjectType.DEPARTMENT.value,
            user.department_id,
        ):
            permissions_by_kb[permission.kb_id] = permission.permission

        for permission in await self.permission_repository.list_by_subject(
            PermissionSubjectType.USER.value,
            str(user.user_id),
        ):
            current = permissions_by_kb.get(permission.kb_id)
            if current is None or permission.permission == "ADMIN":
                permissions_by_kb[permission.kb_id] = permission.permission

        public_kbs = await self.knowledge_base_repository.list_public()
        explicit_kbs = await self.knowledge_base_repository.list_by_ids(permissions_by_kb.keys())
        merged = {kb.id: kb for kb in explicit_kbs}
        merged.update({kb.id: kb for kb in public_kbs})
        return list(merged.values())

    async def upload_document(self, kb_id: int, file: UploadFile, user: CurrentUser) -> KbDocument:
        """上传原始文件、创建文档记录，并提交异步索引任务。"""
        file_name = file.filename
        if not file_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件名不能为空")

        file_type = self._detect_file_type(file_name)
        file_size = self._get_file_size(file)
        if file_size > self.max_upload_file_size_bytes:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件大小超过限制")

        minio_path = await self.storage_service.upload(kb_id, file)
        document: KbDocument | None = None
        try:
            document = await self.document_repository.create(
                kb_id=kb_id,
                file_name=file_name,
                file_type=file_type,
                file_size=file_size,
                minio_path=minio_path,
                uploaded_by=user.user_id,
            )
            await self.index_service.submit_index_task(document.id)
            return document
        except Exception as exc:
            if document is not None:
                await self.document_repository.mark_failed(document.id, str(exc))
            await self.storage_service.delete(minio_path)
            raise

    async def get_index_status(self, kb_id: int, doc_id: int) -> IndexStatusResponse:
        """查询文档索引状态，并带出最近任务的重试次数。"""
        document = await self._get_document_in_kb(kb_id, doc_id)
        latest_task = await self.task_repository.get_latest_by_doc_id(doc_id)
        return IndexStatusResponse(
            doc_id=document.id,
            file_name=document.file_name,
            status=document.status,
            error_msg=document.error_msg,
            chunk_count=document.chunk_count,
            token_count=document.token_count,
            indexed_at=document.indexed_at,
            retry_count=latest_task.retry_count if latest_task is not None else 0,
        )

    async def list_documents(self, kb_id: int) -> list[KbDocument]:
        """查询知识库下未删除的文档记录。"""
        return await self.document_repository.list_by_kb(kb_id)

    async def download_document(self, kb_id: int, doc_id: int) -> tuple[str, bytes]:
        """下载文档原始文件，返回文件名和二进制内容。"""
        document = await self._get_document_in_kb(kb_id, doc_id)
        content = await self.storage_service.download(document.minio_path)
        return document.file_name, content

    async def delete_document(self, kb_id: int, doc_id: int) -> None:
        """软删除文档记录，硬删除 chunk，并删除 MinIO 原文件。"""
        document = await self._get_document_in_kb(kb_id, doc_id)
        await self.document_repository.mark_deleted(doc_id)
        await self.chunk_repository.delete_by_doc_id(doc_id)
        await self.storage_service.delete(document.minio_path)

    async def reindex_document(self, kb_id: int, doc_id: int) -> int:
        """重置文档状态并提交重建索引任务。"""
        await self._get_document_in_kb(kb_id, doc_id)
        await self.document_repository.reset_for_reindex(doc_id)
        return await self.index_service.reindex_document(doc_id)

    def _detect_file_type(self, file_name: str) -> str:
        lower_name = file_name.lower()
        for suffix, file_type in self._supported_extensions.items():
            if lower_name.endswith(suffix):
                return file_type
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不支持的文件类型，目前支持：PDF、DOCX、MD、TXT",
        )

    def _get_file_size(self, file: UploadFile) -> int:
        size = getattr(file, "size", None)
        return int(size) if size is not None else 0

    async def _get_document_in_kb(self, kb_id: int, doc_id: int) -> KbDocument:
        document = await self.document_repository.get(doc_id)
        if document is None or document.is_deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
        if document.kb_id != kb_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
        return document
