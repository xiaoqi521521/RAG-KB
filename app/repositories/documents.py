from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocumentStatus, KbDocument


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class DocumentRepository:
    """`kb_document` 的最小索引阶段数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, doc_id: int) -> KbDocument | None:
        """按文档主键获取单条文档记录。"""
        return await self.session.get(KbDocument, doc_id)

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
        document.indexed_at = _utcnow_naive()
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

    async def reset_for_reindex(self, doc_id: int) -> None:
        """重置文档状态，允许重新进入索引流程。"""
        document = await self.get(doc_id)
        if document is None:
            return
        document.status = DocumentStatus.PENDING.value
        document.error_msg = None
        await self.session.flush()
