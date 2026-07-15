from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Float, case, cast, func, select, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rag import RAG_REFUSAL_ANSWER
from app.models import (
    DocChunk,
    DocumentStatus,
    EvalDataset,
    EvalDatasetStatus,
    EvalResult,
    EvalResultStatus,
    KbDocument,
)


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


@dataclass(frozen=True)
class EvaluationReport:
    """一个知识库评估版本的检索聚合报告。"""

    kb_id: int
    eval_version: str
    total_questions: int
    success_count: int
    partial_count: int
    failed_count: int
    retrieval_sample_count: int
    hit_count: int
    hit_rate_at_5: float | None
    mrr_at_5: float | None
    faithfulness_sample_count: int
    avg_faithfulness: float | None
    answer_relevancy_sample_count: int
    avg_answer_relevancy: float | None
    context_recall_sample_count: int
    avg_context_recall: float | None
    context_precision_sample_count: int
    avg_context_precision: float | None
    refusal_count: int
    refusal_rate: float
    eval_at: datetime


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

    async def version_exists(self, *, kb_id: int, eval_version: str) -> bool:
        """按标准问题所属知识库检查评估版本是否已经存在。"""
        statement = select(
            select(EvalResult.id)
            .join(EvalDataset, EvalResult.dataset_id == EvalDataset.id)
            .where(
                EvalDataset.kb_id == kb_id,
                EvalResult.eval_version == eval_version,
            )
            .exists()
        )
        result = await self.session.execute(statement)
        return bool(result.scalar_one())

    async def save_results(self, results: list[EvalResult]) -> None:
        """在当前请求事务中一次加入并刷新全部逐题结果。"""
        self.session.add_all(results)
        await self.session.flush()

    async def get_report(
        self,
        *,
        kb_id: int,
        eval_version: str,
    ) -> EvaluationReport | None:
        """读取目标知识库的单个评估版本聚合报告。"""
        result = await self.session.execute(
            self._report_statement(kb_id=kb_id).where(EvalResult.eval_version == eval_version)
        )
        row = result.one_or_none()
        return self._report_from_row(kb_id, row) if row is not None else None

    async def list_reports(self, *, kb_id: int) -> list[EvaluationReport]:
        """按评估时间倒序列出目标知识库的版本聚合报告。"""
        result = await self.session.execute(self._report_statement(kb_id=kb_id))
        return [self._report_from_row(kb_id, row) for row in result.all()]

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

    async def invalidate_reindexed_chunk_labels(
        self,
        *,
        kb_id: int,
        old_chunk_ids: list[int],
    ) -> None:
        """使引用旧版本 chunk 的活动标准问题进入待审核状态。

        Args:
            kb_id: 重建文档所属知识库 ID。
            old_chunk_ids: 删除前读取的文档旧版本 chunk ID。

        Returns:
            None。更新保留在当前事务中，由索引发布流程统一提交。
        """
        if not old_chunk_ids:
            return
        await self.session.execute(
            update(EvalDataset)
            .where(
                EvalDataset.kb_id == kb_id,
                EvalDataset.status == EvalDatasetStatus.ACTIVE.value,
                EvalDataset.expected_chunk_ids.overlap(old_chunk_ids),
            )
            .values(
                status=EvalDatasetStatus.NEEDS_REVIEW.value,
                review_reason="document_reindexed",
            )
        )

    def _report_statement(self, *, kb_id: int):
        """构建只按逐题结果实时计算的知识库版本聚合查询。"""
        retrieval_sample_count = func.count(EvalResult.hit)
        hit_count = func.sum(case((EvalResult.hit.is_(True), 1), else_=0))
        reciprocal_rank = case(
            (EvalResult.rank.is_not(None), 1.0 / cast(EvalResult.rank, Float)),
            else_=0.0,
        )
        reciprocal_rank_sum = func.sum(
            case((EvalResult.hit.is_not(None), reciprocal_rank), else_=0.0)
        )
        hit_rate = cast(hit_count, Float) / func.nullif(cast(retrieval_sample_count, Float), 0.0)
        mrr = cast(reciprocal_rank_sum, Float) / func.nullif(
            cast(retrieval_sample_count, Float), 0.0
        )
        total_questions = func.count(EvalResult.id)
        refusal_count = func.sum(
            case((EvalResult.actual_answer == RAG_REFUSAL_ANSWER, 1), else_=0)
        )
        refusal_rate = cast(refusal_count, Float) / func.nullif(
            cast(total_questions, Float), 0.0
        )
        evaluated_at = func.max(EvalResult.eval_at)

        return (
            select(
                EvalResult.eval_version.label("eval_version"),
                total_questions.label("total_questions"),
                func.sum(
                    case((EvalResult.status == EvalResultStatus.SUCCESS.value, 1), else_=0)
                ).label("success_count"),
                func.sum(
                    case((EvalResult.status == EvalResultStatus.PARTIAL.value, 1), else_=0)
                ).label("partial_count"),
                func.sum(
                    case((EvalResult.status == EvalResultStatus.FAILED.value, 1), else_=0)
                ).label("failed_count"),
                retrieval_sample_count.label("retrieval_sample_count"),
                hit_count.label("hit_count"),
                hit_rate.label("hit_rate_at_5"),
                mrr.label("mrr_at_5"),
                func.count(EvalResult.faithfulness).label("faithfulness_sample_count"),
                func.avg(EvalResult.faithfulness).label("avg_faithfulness"),
                func.count(EvalResult.answer_relevancy).label(
                    "answer_relevancy_sample_count"
                ),
                func.avg(EvalResult.answer_relevancy).label("avg_answer_relevancy"),
                func.count(EvalResult.context_recall).label("context_recall_sample_count"),
                func.avg(EvalResult.context_recall).label("avg_context_recall"),
                func.count(EvalResult.context_precision).label(
                    "context_precision_sample_count"
                ),
                func.avg(EvalResult.context_precision).label("avg_context_precision"),
                refusal_count.label("refusal_count"),
                refusal_rate.label("refusal_rate"),
                evaluated_at.label("eval_at"),
            )
            .join(EvalDataset, EvalResult.dataset_id == EvalDataset.id)
            .where(EvalDataset.kb_id == kb_id)
            .group_by(EvalResult.eval_version)
            .order_by(evaluated_at.desc())
        )

    def _report_from_row(self, kb_id: int, row: Row[Any]) -> EvaluationReport:
        """把 SQLAlchemy 聚合行转换为稳定的报告对象。"""
        values = row._mapping
        return EvaluationReport(
            kb_id=kb_id,
            eval_version=values["eval_version"],
            total_questions=values["total_questions"],
            success_count=values["success_count"],
            partial_count=values["partial_count"],
            failed_count=values["failed_count"],
            retrieval_sample_count=values["retrieval_sample_count"],
            hit_count=values["hit_count"],
            hit_rate_at_5=values["hit_rate_at_5"],
            mrr_at_5=values["mrr_at_5"],
            faithfulness_sample_count=values["faithfulness_sample_count"],
            avg_faithfulness=values["avg_faithfulness"],
            answer_relevancy_sample_count=values["answer_relevancy_sample_count"],
            avg_answer_relevancy=values["avg_answer_relevancy"],
            context_recall_sample_count=values["context_recall_sample_count"],
            avg_context_recall=values["avg_context_recall"],
            context_precision_sample_count=values["context_precision_sample_count"],
            avg_context_precision=values["avg_context_precision"],
            refusal_count=values["refusal_count"],
            refusal_rate=values["refusal_rate"],
            eval_at=values["eval_at"],
        )
