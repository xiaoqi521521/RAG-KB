from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocumentStatus, KbDocument


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class DocumentRepository:
    """`kb_document` 的最小索引阶段数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, doc_id: int) -> KbDocument | None:
        return await self.session.get(KbDocument, doc_id)

    async def mark_processing(self, doc_id: int) -> None:
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
        document = await self.get(doc_id)
        if document is None:
            return
        document.status = DocumentStatus.FAILED.value
        document.error_msg = error_msg
        await self.session.flush()
