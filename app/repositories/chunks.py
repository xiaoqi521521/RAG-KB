from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocChunk


class ChunkRepository:
    """`kb_doc_chunk` 的最小索引阶段数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_many(self, chunks: list[DocChunk]) -> None:
        """批量写入文档分块，并在当前事务中刷新主键。"""
        if not chunks:
            return
        self.session.add_all(chunks)
        await self.session.flush()

    async def delete_older_versions(self, doc_id: int, current_version: int) -> None:
        """删除指定文档在当前版本之前的历史分块数据。"""
        await self.session.execute(
            delete(DocChunk).where(
                DocChunk.doc_id == doc_id,
                DocChunk.doc_version < current_version,
            )
        )
        await self.session.flush()

    async def delete_by_doc_id(self, doc_id: int) -> None:
        """删除指定文档关联的全部分块记录。"""
        await self.session.execute(delete(DocChunk).where(DocChunk.doc_id == doc_id))
        await self.session.flush()
