from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.core.context import CurrentUser
from app.core.rag import RAG_REFUSAL_ANSWER
from app.core.time import shanghai_now_naive
from app.evaluation.metrics import calculate_retrieval_metrics
from app.evaluation.ragas_evaluator import (
    RagasEvaluationResult,
    RagasEvaluationSample,
)
from app.models import EvalDataset, EvalDatasetStatus, EvalResult, EvalResultStatus
from app.repositories.evaluations import EvaluationReport, EvaluationRepository
from app.services.rag_query_v4 import RagExecution

logger = logging.getLogger(__name__)


class EvaluationRagExecutor(Protocol):
    """正式评估依赖的共享 V4 执行接口。"""

    async def execute(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
    ) -> RagExecution: ...


class GenerationEvaluator(Protocol):
    """正式评估依赖的四项生成指标接口。"""

    async def evaluate(self, sample: RagasEvaluationSample) -> RagasEvaluationResult: ...


class EvaluationRunService:
    """串行执行标准问题并在最后原子保存全部逐题结果。"""

    def __init__(
        self,
        *,
        repository: EvaluationRepository,
    ) -> None:
        self.repository = repository

    async def run(
        self,
        *,
        kb_id: int,
        eval_version: str,
        user: CurrentUser,
        rag_executor: EvaluationRagExecutor,
        ragas_evaluator: GenerationEvaluator,
    ) -> EvaluationReport:
        """同步运行当前 V4 管道，并返回本次版本聚合报告。"""
        if await self.repository.version_exists(kb_id=kb_id, eval_version=eval_version):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="评估版本已存在",
            )

        datasets = await self.repository.list_datasets(
            kb_id=kb_id,
            status=EvalDatasetStatus.ACTIVE.value,
        )
        if not datasets:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="没有可运行的 ACTIVE 标准问题",
            )

        evaluated_at = shanghai_now_naive()
        results: list[EvalResult] = []
        for dataset in datasets:
            results.append(
                await self._evaluate_dataset(
                    kb_id=kb_id,
                    eval_version=eval_version,
                    dataset=dataset,
                    user=user,
                    rag_executor=rag_executor,
                    ragas_evaluator=ragas_evaluator,
                    evaluated_at=evaluated_at,
                )
            )

        try:
            await self.repository.save_results(results)
        except IntegrityError as exc:
            # 并发运行由数据库唯一约束兜底，整个请求事务随后统一回滚。
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="评估版本已存在",
            ) from exc

        report = await self.repository.get_report(kb_id=kb_id, eval_version=eval_version)
        if report is None:
            raise RuntimeError("evaluation report missing after result persistence")
        return report

    async def list_history(self, *, kb_id: int) -> list[EvaluationReport]:
        """读取目标知识库按时间倒序排列的聚合历史。"""
        return await self.repository.list_reports(kb_id=kb_id)

    async def _evaluate_dataset(
        self,
        *,
        kb_id: int,
        eval_version: str,
        dataset: EvalDataset,
        user: CurrentUser,
        rag_executor: EvaluationRagExecutor,
        ragas_evaluator: GenerationEvaluator,
        evaluated_at: datetime,
    ) -> EvalResult:
        """隔离单题主流程异常，并形成一条内存结果。"""
        try:
            execution = await rag_executor.execute(
                question=dataset.question,
                kb_ids=[kb_id],
                user=user,
            )
        except Exception as exc:
            logger.warning(
                "Evaluation question failed: error_type=rag_execution_failed exception_type=%s",
                type(exc).__name__,
            )
            return EvalResult(
                dataset_id=dataset.id,
                eval_version=eval_version,
                hit=None,
                rank=None,
                actual_answer=None,
                status=EvalResultStatus.FAILED.value,
                error_type="rag_execution_failed",
                eval_at=evaluated_at,
            )

        if execution.reranker_degraded:
            logger.info(
                "Evaluation question skipped after Reranker degradation: reason=%s",
                execution.degraded_reason,
            )
            return EvalResult(
                dataset_id=dataset.id,
                eval_version=eval_version,
                hit=None,
                rank=None,
                actual_answer=execution.public_response.answer,
                status=EvalResultStatus.PARTIAL.value,
                error_type="reranker_degraded",
                eval_at=evaluated_at,
            )

        actual_answer = (
            RAG_REFUSAL_ANSWER
            if execution.explicit_refusal
            else execution.public_response.answer
        )
        hit: bool | None = None
        rank: int | None = None
        if dataset.expected_chunk_ids:
            metrics = calculate_retrieval_metrics(
                [item.chunk_id for item in execution.reranked_hits],
                dataset.expected_chunk_ids,
            )
            hit = metrics.hit
            rank = metrics.rank

        faithfulness: float | None = None
        answer_relevancy: float | None = None
        context_recall: float | None = None
        context_precision: float | None = None
        result_status = EvalResultStatus.SUCCESS.value
        error_type: str | None = None
        expected_answer = dataset.expected_answer.strip() if dataset.expected_answer else ""
        if expected_answer and not execution.explicit_refusal:
            try:
                generation = await ragas_evaluator.evaluate(
                    RagasEvaluationSample(
                        question=dataset.question,
                        actual_answer=actual_answer,
                        expected_answer=expected_answer,
                        reference_contexts=execution.reference_contexts,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "RAGAS question evaluation failed: error_type=ragas_metric_failed "
                    "exception_type=%s",
                    type(exc).__name__,
                )
                result_status = EvalResultStatus.PARTIAL.value
                error_type = "ragas_metric_failed"
            else:
                faithfulness = generation.faithfulness
                answer_relevancy = generation.answer_relevancy
                context_recall = generation.context_recall
                context_precision = generation.context_precision
                scores = (
                    faithfulness,
                    answer_relevancy,
                    context_recall,
                    context_precision,
                )
                if generation.errors or any(score is None for score in scores):
                    result_status = EvalResultStatus.PARTIAL.value
                    error_type = "ragas_metric_failed"
                    logger.info(
                        "RAGAS question evaluation partial: failed_metrics=%s error_types=%s",
                        ",".join(error.metric.value for error in generation.errors) or "unknown",
                        ",".join(error.error_type.value for error in generation.errors)
                        or "unknown",
                    )

        return EvalResult(
            dataset_id=dataset.id,
            eval_version=eval_version,
            hit=hit,
            rank=rank,
            actual_answer=actual_answer,
            faithfulness=faithfulness,
            answer_relevancy=answer_relevancy,
            context_recall=context_recall,
            context_precision=context_precision,
            status=result_status,
            error_type=error_type,
            eval_at=evaluated_at,
        )
