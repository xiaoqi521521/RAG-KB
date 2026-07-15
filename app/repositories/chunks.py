from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy import Float
from sqlalchemy import literal_column
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocChunk, DocumentStatus, KbDocument


@dataclass(frozen=True)
class ChunkSearchHit:
    """向量检索命中的 chunk 及其来源信息。

    Args:
        chunk_id: 命中的 chunk ID。
        doc_id: chunk 所属文档 ID。
        document_name: chunk 所属文档名称。
        kb_id: chunk 所属知识库 ID。
        chunk_index: chunk 在文档中的序号。
        content: chunk 文本内容。
        page_num: 来源页码；非分页文档为 None。
        section_title: 来源章节标题；无法识别章节时为 None。
        score: 由向量距离换算得到的相似度分数。
    """

    chunk_id: int
    doc_id: int
    document_name: str
    kb_id: int
    chunk_index: int
    content: str
    page_num: int | None
    section_title: str | None
    score: float


class ChunkRepository:
    """`kb_doc_chunk` 的最小索引阶段数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_by_vector(
        self,
        *,
        query_vector: list[float],
        kb_ids: list[int],
        top_k: int,
    ) -> list[ChunkSearchHit]:
        """按向量相似度检索当前可用版本的 chunk。

        Args:
            query_vector: 用户问题向量，必须与建库向量维度一致。
            kb_ids: 已通过读权限校验的知识库 ID 列表。
            top_k: 数据库召回数量上限。

        Returns:
            按相似度从高到低排序的 ChunkSearchHit 列表。
        """
        if not kb_ids or top_k <= 0:
            return []

        # `<=>` 返回的是距离浮点数，但 SQLAlchemy 会沿用左侧 Vector 类型；
        # 显式标成 Float，避免 pgvector 结果处理器把距离值当向量解析。
        distance_expr = type_coerce(DocChunk.embedding.op("<=>")(query_vector), Float).label("distance")
        statement = (
            select(
                DocChunk.id,
                DocChunk.doc_id,
                KbDocument.file_name,
                DocChunk.kb_id,
                DocChunk.chunk_index,
                DocChunk.content,
                DocChunk.page_num,
                DocChunk.section_title,
                distance_expr,
            )
            .join(KbDocument, DocChunk.doc_id == KbDocument.id)
            .where(
                DocChunk.kb_id.in_(kb_ids),
                # 查询侧只允许召回文档当前完成版本，避免重建过程中的半成品 chunk 泄漏。
                DocChunk.doc_version == KbDocument.version,
                KbDocument.status == DocumentStatus.DONE.value,
                KbDocument.is_deleted.is_(False),
            )
            .order_by(distance_expr)
            .limit(top_k)
        )
        result = await self.session.execute(statement)
        return [
            ChunkSearchHit(
                chunk_id=row[0],
                doc_id=row[1],
                document_name=row[2],
                kb_id=row[3],
                chunk_index=row[4],
                content=row[5],
                page_num=row[6],
                section_title=row[7],
                score=1 / (1 + float(row[8])),
            )
            for row in result.all()
        ]

    async def search_by_fulltext(
        self,
        *,
        query_text: str,
        kb_ids: list[int],
        top_k: int,
    ) -> list[ChunkSearchHit]:
        """按 PostgreSQL 全文检索召回当前可用版本的 chunk。

        Args:
            query_text: 已清洗并按 `&` 拼接的 tsquery 文本，传给 to_tsquery。
            kb_ids: 已通过读权限校验的知识库 ID 列表。
            top_k: 数据库召回数量上限。

        Returns:
            按全文 rank 从高到低排序的 ChunkSearchHit 列表。
        """
        if not query_text.strip() or not kb_ids or top_k <= 0:
            return []

        ts_config = literal_column("'simple'")
        query_expr = func.to_tsquery(ts_config, query_text)
        rank_expr = type_coerce(func.ts_rank(DocChunk.content_tsv, query_expr), Float).label("rank")

        statement = (
            select(
                DocChunk.id,
                DocChunk.doc_id,
                KbDocument.file_name,
                DocChunk.kb_id,
                DocChunk.chunk_index,
                DocChunk.content,
                DocChunk.page_num,
                DocChunk.section_title,
                rank_expr,
            )
            .join(KbDocument, DocChunk.doc_id == KbDocument.id)
            .where(
                DocChunk.kb_id.in_(kb_ids),
                DocChunk.doc_version == KbDocument.version,
                KbDocument.status == DocumentStatus.DONE.value,
                KbDocument.is_deleted.is_(False),
                DocChunk.content_tsv.op("@@")(query_expr),
            )
            .order_by(rank_expr.desc())
            .limit(top_k)
        )
        result = await self.session.execute(statement)
        return [
            ChunkSearchHit(
                chunk_id=row[0],
                doc_id=row[1],
                document_name=row[2],
                kb_id=row[3],
                chunk_index=row[4],
                content=row[5],
                page_num=row[6],
                section_title=row[7],
                score=float(row[8]),
            )
            for row in result.all()
        ]

    async def insert_many(self, chunks: list[DocChunk]) -> None:
        """批量写入文档分块，并在当前事务中刷新主键。"""
        if not chunks:
            return
        self.session.add_all(chunks)
        await self.session.flush()

    async def list_older_version_ids(self, doc_id: int, current_version: int) -> list[int]:
        """读取指定文档在当前版本之前的历史 chunk ID。"""
        result = await self.session.execute(
            select(DocChunk.id).where(
                DocChunk.doc_id == doc_id,
                DocChunk.doc_version < current_version,
            )
        )
        return list(result.scalars().all())

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
