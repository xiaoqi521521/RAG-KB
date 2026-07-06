from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import shanghai_now_naive
from app.models import DocumentStatus, KbDocument


class DocumentRepository:
    """`kb_document` 的最小索引阶段数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, doc_id: int) -> KbDocument | None:
        """按文档主键获取单条文档记录。"""
        return await self.session.get(KbDocument, doc_id)

    async def get_active_in_kb(self, kb_id: int, doc_id: int) -> KbDocument | None:
        """查询指定知识库下未删除的文档。

        Args:
            kb_id: 文档应归属的知识库 ID。
            doc_id: 待查询文档 ID。

        Returns:
            匹配且未删除的文档；不存在、已删除或知识库不匹配时返回 None。
        """
        result = await self.session.execute(
            select(KbDocument).where(
                KbDocument.id == doc_id,
                KbDocument.kb_id == kb_id,
                KbDocument.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        kb_id: int,
        file_name: str,
        file_type: str,
        file_size: int,
        minio_path: str,
        uploaded_by: int,
    ) -> KbDocument:
        """创建待索引文档记录并返回持久化后的实体。"""
        document = KbDocument(
            kb_id=kb_id,
            file_name=file_name,
            file_type=file_type,
            file_size=file_size,
            minio_path=minio_path,
            uploaded_by=uploaded_by,
            status=DocumentStatus.PENDING.value,
        )
        self.session.add(document)
        await self.session.flush()
        return document

    async def list_by_kb(self, kb_id: int) -> list[KbDocument]:
        """列出指定知识库下未删除的文档。"""
        result = await self.session.execute(
            select(KbDocument).where(
                KbDocument.kb_id == kb_id,
                KbDocument.is_deleted.is_(False),
            )
        )
        return list(result.scalars().all())

    async def mark_processing(self, doc_id: int) -> None:
        """将文档状态更新为处理中，并清空历史错误信息。"""
        document = await self.get(doc_id)
        if document is None:
            return
        document.status = DocumentStatus.PROCESSING.value
        document.error_msg = None
        await self.session.flush()

    async def mark_done(
        self,
        doc_id: int,
        *,
        chunk_count: int,
        token_count: int,
        version: int,
    ) -> None:
        """将文档状态更新为完成，并写入索引结果统计。"""
        document = await self.get(doc_id)
        if document is None:
            return
        document.status = DocumentStatus.DONE.value
        document.error_msg = None
        document.chunk_count = chunk_count
        document.token_count = token_count
        document.version = version
        document.indexed_at = shanghai_now_naive()
        await self.session.flush()

    async def mark_failed(self, doc_id: int, error_msg: str) -> None:
        """将文档状态更新为失败，并记录失败原因。"""
        document = await self.get(doc_id)
        if document is None:
            return
        document.status = DocumentStatus.FAILED.value
        document.error_msg = error_msg
        await self.session.flush()

    async def mark_deleted(self, doc_id: int) -> None:
        """将文档标记为删除，供后续查询过滤。"""
        document = await self.get(doc_id)
        if document is None:
            return
        document.is_deleted = True
        await self.session.flush()

    async def replace_file_and_reset_index(
        self,
        doc_id: int,
        *,
        file_name: str,
        file_type: str,
        file_size: int,
        minio_path: str,
    ) -> KbDocument:
        """替换文档原文件元数据，并重置本轮重建索引状态。

        Args:
            doc_id: 待替换文档 ID。
            file_name: 新文件名。
            file_type: 根据新文件名识别出的文件类型。
            file_size: 新文件大小，单位为字节。
            minio_path: 新文件在 MinIO 中的对象路径。

        Returns:
            更新后的文档实体。
        """
        document = await self.get(doc_id)
        if document is None:
            raise ValueError(f"document not found: {doc_id}")

        document.file_name = file_name
        document.file_type = file_type
        document.file_size = file_size
        document.minio_path = minio_path
        self._reset_index_fields(document)
        await self.session.flush()
        return document

    async def reset_for_reindex(self, doc_id: int) -> None:
        """重置文档状态和旧统计字段，允许重新进入索引流程。"""
        document = await self.get(doc_id)
        if document is None:
            return
        self._reset_index_fields(document)
        await self.session.flush()

    def _reset_index_fields(self, document: KbDocument) -> None:
        """清理旧索引状态，避免前端把上一轮统计误认为新任务结果。"""
        document.status = DocumentStatus.PENDING.value
        document.error_msg = None
        document.chunk_count = None
        document.token_count = None
        document.indexed_at = None
