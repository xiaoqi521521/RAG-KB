from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocChunk


class ChunkRepository:
    """`kb_doc_chunk` 的最小索引阶段数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_many(self, chunks: list[DocChunk]) -> None:
        if not chunks:
            return
        self.session.add_all(chunks)
        await self.session.flush()

    async def delete_older_versions(self, doc_id: int, current_version: int) -> None:
        await self.session.execute(
            delete(DocChunk).where(
                DocChunk.doc_id == doc_id,
                DocChunk.doc_version < current_version,
            )
        )
        await self.session.flush()
