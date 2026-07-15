from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocChunk, DocumentStatus, EvalDataset, EvalResult, KbDocument


@dataclass(frozen=True)
class CurrentChunkSummary:
    """供标准问题标注使用的当前 chunk 摘要。"""

    chunk_id: int
    document_id: int
    document_name: str
    chunk_index: int
    page_number: int | None
    section_title: str | None
    token_count: int
    excerpt: str


class EvaluationRepository:
    """标准问题集、评估结果关联和标注 chunk 的数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_datasets(
        self,
        *,
        kb_id: int,
        status: str | None = None,
    ) -> list[EvalDataset]:
        """按知识库和可选状态列出标准问题。"""
        statement = select(EvalDataset).where(EvalDataset.kb_id == kb_id)
        if status is not None:
            statement = statement.where(EvalDataset.status == status)
        result = await self.session.execute(
            statement.order_by(EvalDataset.created_at.desc(), EvalDataset.id.desc())
        )
        return list(result.scalars().all())

    async def get_dataset(self, *, kb_id: int, dataset_id: int) -> EvalDataset | None:
        """同时按知识库和数据集 ID 获取标准问题。"""
        result = await self.session.execute(
            select(EvalDataset).where(
                EvalDataset.kb_id == kb_id,
                EvalDataset.id == dataset_id,
            )
        )
        return result.scalar_one_or_none()

    async def create_dataset(
        self,
        *,
        kb_id: int,
        question: str,
        expected_answer: str | None,
        expected_chunk_ids: list[int] | None,
        status: str,
        created_by: int,
        review_reason: str | None = None,
        source_feedback_id: int | None = None,
    ) -> EvalDataset:
        """创建标准问题并刷新数据库生成字段。"""
        dataset = EvalDataset(
            kb_id=kb_id,
            question=question,
            expected_answer=expected_answer,
            expected_chunk_ids=expected_chunk_ids,
            status=status,
            created_by=created_by,
            review_reason=review_reason,
            source_feedback_id=source_feedback_id,
        )
        self.session.add(dataset)
        await self.session.flush()
        return dataset

    async def save_dataset(self, dataset: EvalDataset) -> EvalDataset:
        """刷新已修改的标准问题实体。"""
        await self.session.flush()
        return dataset

    async def has_results(self, dataset_id: int) -> bool:
        """判断标准问题是否已经被任一评估结果引用。"""
        statement = select(
            select(EvalResult.id).where(EvalResult.dataset_id == dataset_id).exists()
        )
        result = await self.session.execute(statement)
        return bool(result.scalar_one())

    async def list_valid_chunk_ids(
        self,
        *,
        kb_id: int,
        chunk_ids: list[int],
    ) -> list[int]:
        """返回属于当前知识库已发布文档版本的指定 chunk ID。"""
        if not chunk_ids:
            return []
        result = await self.session.execute(
            select(DocChunk.id)
            .join(KbDocument, DocChunk.doc_id == KbDocument.id)
            .where(
                DocChunk.id.in_(chunk_ids),
                DocChunk.kb_id == kb_id,
                KbDocument.kb_id == kb_id,
                DocChunk.doc_version == KbDocument.version,
                KbDocument.status == DocumentStatus.DONE.value,
                KbDocument.is_deleted.is_(False),
            )
        )
        return list(result.scalars().all())

    async def list_current_chunk_summaries(self, *, kb_id: int) -> list[CurrentChunkSummary]:
        """查询当前已发布 chunk 的位置、Token 和最多 200 字摘要。"""
        excerpt = func.left(DocChunk.content, 200).label("excerpt")
        result = await self.session.execute(
            select(
                DocChunk.id,
                KbDocument.id,
                KbDocument.file_name,
                DocChunk.chunk_index,
                DocChunk.page_num,
                DocChunk.section_title,
                DocChunk.token_count,
                excerpt,
            )
            .join(KbDocument, DocChunk.doc_id == KbDocument.id)
            .where(
                DocChunk.kb_id == kb_id,
                KbDocument.kb_id == kb_id,
                DocChunk.doc_version == KbDocument.version,
                KbDocument.status == DocumentStatus.DONE.value,
                KbDocument.is_deleted.is_(False),
            )
            .order_by(KbDocument.id, DocChunk.chunk_index)
        )
        return [
            CurrentChunkSummary(
                chunk_id=row[0],
                document_id=row[1],
                document_name=row[2],
                chunk_index=row[3],
                page_number=row[4],
                section_title=row[5],
                token_count=row[6],
                excerpt=row[7],
            )
            for row in result.all()
        ]
