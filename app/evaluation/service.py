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
        eval_version: int | None = None,
        user: CurrentUser,
        rag_executor: EvaluationRagExecutor,
        ragas_evaluator: GenerationEvaluator,
    ) -> EvaluationReport:
        """同步运行当前 V4 管道，并返回本次版本聚合报告。"""
        logger.info("Evaluation run starting: kb_id=%s user_id=%s", kb_id, user.user_id)

        # 版本由数据库中的历史最大值递增生成，避免前端或请求参数覆盖版本顺序。
        if eval_version is None:
            eval_version = await self.repository.next_report_version(kb_id=kb_id)

        logger.info("Evaluation version determined: kb_id=%s eval_version=%s", kb_id, eval_version)

        if await self.repository.version_exists(kb_id=kb_id, eval_version=eval_version):
            logger.warning(
                "Evaluation version conflict: kb_id=%s eval_version=%s",
                kb_id,
                eval_version,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="评估版本已存在",
            )

        datasets = await self.repository.list_datasets(
            kb_id=kb_id,
            status=EvalDatasetStatus.ACTIVE.value,
        )
        if not datasets:
            # 检查是否有其他状态的数据集
            all_datasets = await self.repository.list_datasets(kb_id=kb_id)
            logger.warning(
                "No ACTIVE datasets found: kb_id=%s total_datasets=%s",
                kb_id,
                len(all_datasets),
            )
            if not all_datasets:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="知识库中没有任何评估数据集，请先添加标准问题",
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"没有可运行的 ACTIVE 标准问题（当前有 {len(all_datasets)} 个非 ACTIVE 状态的问题），请将至少一个问题设置为有效状态",
            )

        logger.info(
            "Starting evaluation execution: kb_id=%s eval_version=%s dataset_count=%s",
            kb_id,
            eval_version,
            len(datasets),
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

    async def list_history(
        self,
        *,
        kb_id: int,
        eval_version: int | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[EvaluationReport]:
        """读取目标知识库按时间倒序排列的聚合历史，可按版本过滤和分页。"""
        if eval_version is None and limit is None and offset == 0:
            # 保留仓储层旧调用形态，便于离线评估和轻量替身复用。
            return await self.repository.list_reports(kb_id=kb_id)
        if eval_version is None:
            return await self.repository.list_reports(kb_id=kb_id, limit=limit, offset=offset)
        return await self.repository.list_reports(
            kb_id=kb_id,
            eval_version=eval_version,
            limit=limit,
            offset=offset,
        )

    async def count_history(self, *, kb_id: int, eval_version: int | None = None) -> int:
        """统计评估历史记录数量。"""
        return await self.repository.count_reports(kb_id=kb_id, eval_version=eval_version)

    async def list_history_versions(self, *, kb_id: int) -> list[int]:
        """列出全部评估版本。"""
        return await self.repository.list_report_versions(kb_id=kb_id)

    async def _evaluate_dataset(
        self,
        *,
        kb_id: int,
        eval_version: int,
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
