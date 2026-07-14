from __future__ import annotations

import logging
from typing import Protocol

from fastapi import HTTPException, UploadFile, status

from app.core.context import CurrentUser
from app.models import DocumentStatus, KbDocument
from app.repositories.documents import DocumentRepository
from app.schemas.knowledge_base import DocumentReindexSubmitResponse
from app.services.indexing import IndexService

logger = logging.getLogger(__name__)


class DocumentObjectStorage(Protocol):
    """文档更新流程依赖的对象存储最小接口。"""

    async def upload(self, kb_id: int, file: UploadFile) -> str:
        """上传原始文件并返回对象路径。"""
        ...

    async def delete(self, object_key: str) -> None:
        """删除指定对象路径。"""
        ...


class DocumentUpdateService:
    """文档更新服务，负责编排文档替换和强制重建索引。"""

    _supported_extensions = {
        ".pdf": "PDF",
        ".docx": "DOCX",
        ".md": "MD",
        ".txt": "TXT",
    }

    def __init__(
        self,
        *,
        document_repository: DocumentRepository,
        storage_service: DocumentObjectStorage,
        index_service: IndexService,
        max_upload_file_size_mb: int,
    ) -> None:
        """初始化文档更新服务依赖。

        Args:
            document_repository: 文档元数据仓储。
            storage_service: MinIO 存储服务，需提供 upload/delete。
            index_service: 索引服务，用于提交 REINDEX 任务。
            max_upload_file_size_mb: 允许上传的最大文件大小，单位 MB。
        """
        self.document_repository = document_repository
        self.storage_service = storage_service
        self.index_service = index_service
        self.max_upload_file_size_bytes = max_upload_file_size_mb * 1024 * 1024

    async def replace_content(
        self,
        kb_id: int,
        doc_id: int,
        file: UploadFile,
        user: CurrentUser,
    ) -> DocumentReindexSubmitResponse:
        """替换已有文档原文件，并提交新版本重建索引任务。

        Args:
            kb_id: 文档所属知识库 ID。
            doc_id: 待替换文档 ID。
            file: 新上传的原始文件。
            user: 当前用户上下文，用于日志追踪。

        Returns:
            包含文档 ID、文件名、状态和任务 ID 的提交结果。
        """
        document = await self._get_editable_document(kb_id, doc_id)
        file_name = self._require_file_name(file)
        file_type = self._detect_file_type(file_name)
        file_size = self._require_file_size(file)
        old_minio_path = document.minio_path
        new_minio_path: str | None = None

        try:
            # 新文件只放入任务 payload，索引成功前不覆盖当前发布版本。
            new_minio_path = await self.storage_service.upload(kb_id, file)
            task_id = await self.index_service.reindex_document(
                doc_id,
                payload={
                    "source": {
                        "file_name": file_name,
                        "file_type": file_type,
                        "file_size": file_size,
                        "minio_path": new_minio_path,
                    },
                    "old_minio_path": old_minio_path,
                },
            )
        except Exception:
            if new_minio_path is not None:
                await self.storage_service.delete(new_minio_path)
            raise

        logger.info("[DocumentUpdate] 文档内容替换任务已提交")
        return self._submitted_response(
            document,
            task_id,
            "文档替换任务已提交，新版本索引完成前继续使用当前可查询版本",
        )

    async def force_reindex(self, kb_id: int, doc_id: int) -> DocumentReindexSubmitResponse:
        """不替换原文件，直接提交文档强制重建索引任务。

        Args:
            kb_id: 文档所属知识库 ID。
            doc_id: 待强制重建索引的文档 ID。

        Returns:
            包含文档 ID、文件名、状态和任务 ID 的提交结果。
        """
        document = await self._get_editable_document(kb_id, doc_id)
        try:
            if document.status != DocumentStatus.DONE.value:
                await self.document_repository.reset_for_reindex(doc_id)
            task_id = await self.index_service.reindex_document(doc_id)
        except Exception as exc:
            if document.status != DocumentStatus.DONE.value:
                await self.document_repository.mark_failed(doc_id, str(exc))
            raise

        logger.info("[DocumentUpdate] 强制重建索引已提交")
        return self._submitted_response(
            document,
            task_id,
            "强制重建索引任务已提交，已发布版本在重建期间继续可查询",
        )

    async def _get_editable_document(self, kb_id: int, doc_id: int) -> KbDocument:
        """读取可更新文档，并拒绝并发重建状态。

        Args:
            kb_id: 文档应归属的知识库 ID。
            doc_id: 待更新文档 ID。

        Returns:
            可进入替换或强制重建流程的文档。
        """
        document = await self.document_repository.get_active_in_kb(kb_id, doc_id)
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
        if document.status in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="文档正在索引中")
        return document

    def _require_file_name(self, file: UploadFile) -> str:
        """校验上传文件名。

        Args:
            file: FastAPI 上传文件对象。

        Returns:
            非空文件名。
        """
        file_name = file.filename
        if not file_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件名不能为空")
        return file_name

    def _detect_file_type(self, file_name: str) -> str:
        """根据文件名后缀识别文档类型。

        Args:
            file_name: 上传文件名。

        Returns:
            标准化文档类型。
        """
        lower_name = file_name.lower()
        for suffix, file_type in self._supported_extensions.items():
            if lower_name.endswith(suffix):
                return file_type
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不支持的文件类型，目前支持：PDF、DOCX、MD、TXT",
        )

    def _require_file_size(self, file: UploadFile) -> int:
        """校验上传文件大小，避免未知大小绕过限制。

        Args:
            file: FastAPI 上传文件对象。

        Returns:
            文件大小，单位字节。
        """
        size = getattr(file, "size", None)
        if size is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件大小未知")
        file_size = int(size)
        if file_size > self.max_upload_file_size_bytes:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件大小超过限制")
        return file_size

    def _submitted_response(
        self,
        document: KbDocument,
        task_id: int,
        message: str,
    ) -> DocumentReindexSubmitResponse:
        """组装文档更新任务提交响应。"""
        return DocumentReindexSubmitResponse(
            doc_id=document.id,
            file_name=document.file_name,
            status=document.status,
            task_id=task_id,
            message=message,
        )
