from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
import time
from typing import Protocol

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.core.context import CurrentUser
from app.core.rag import RAG_REFUSAL_ANSWER
from app.core.time import shanghai_now_naive
from app.evaluation.metrics import calculate_retrieval_metrics
from app.evaluation.ragas_evaluator import (
    RagasUsage,
    RagasEvaluationResult,
    RagasEvaluationSample,
)
from app.models import (
    EvalDataset,
    EvalDatasetStatus,
    EvalResult,
    EvalResultStatus,
    EvalRunUsage,
)
from app.repositories.evaluations import EvaluationReport, EvaluationRepository
from app.services.token_budget import (
    GlobalTokenBudgetGate,
    TokenBudgetExhaustedError,
    TokenBudgetUnavailableError,
)
from app.services.token_metrics import TokenUsageRecorder
from app.services.rag_query_v4 import RagExecution

logger = logging.getLogger(__name__)


@dataclass
class EvaluationUsageCollector:
    """按题累计 RAG 执行期间的模型消耗，run 完成后统一汇总。"""

    usage_tokens: int = 0
    estimated_cost_cny: Decimal = Decimal("0")

    def add(self, *, tokens: int, cost_cny: Decimal) -> None:
        self.usage_tokens += tokens
        self.estimated_cost_cny += cost_cny


@dataclass(frozen=True)
class _RagPhase:
    """单题 RAG 执行后的中间状态和可复用输入。"""

    eval_version: int
    dataset: EvalDataset
    execution: RagExecution | None
    result: EvalResult | None = None
    sample: RagasEvaluationSample | None = None
    actual_answer: str | None = None
    hit: bool | None = None
    rank: int | None = None
    evaluated_at: datetime | None = None
    usage_collector: EvaluationUsageCollector | None = None


class EvaluationRagExecutor(Protocol):
    """正式评估依赖的 V4 单题执行接口，实现必须支持并发调用。"""

    async def execute(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
        usage_collector: EvaluationUsageCollector,
    ) -> RagExecution: ...


class GenerationEvaluator(Protocol):
    """正式评估依赖的四项生成指标接口。"""

    async def evaluate(self, sample: RagasEvaluationSample) -> RagasEvaluationResult: ...

    @property
    def usage(self) -> RagasUsage: ...


class EvaluationRunService:
    """有界并行执行 RAG，并并发执行题目级 RAGAS 后原子保存结果与 run 用量。"""

    def __init__(
        self,
        *,
        repository: EvaluationRepository,
        rag_concurrency: int = 4,
        token_metrics: TokenUsageRecorder | None = None,
        budget_gate: GlobalTokenBudgetGate | None = None,
        token_chat_model_name: str = "unknown",
        token_embedding_model_name: str = "unknown",
    ) -> None:
        if isinstance(rag_concurrency, bool) or not 1 <= rag_concurrency <= 16:
            raise ValueError("rag_concurrency must be between 1 and 16")
        self.repository = repository
        self.rag_concurrency = rag_concurrency
        self.token_metrics = token_metrics
        self.budget_gate = budget_gate
        self.token_chat_model_name = token_chat_model_name
        self.token_embedding_model_name = token_embedding_model_name

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

        budget_gate = self.budget_gate
        if budget_gate is not None:
            # 批量评估消耗集中，启动前先检查全局预算，避免跑分打爆在线问答额度。
            try:
                await budget_gate.ensure_available()
            except TokenBudgetExhaustedError as exc:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="今日金额预算已用尽",
                ) from exc
            except TokenBudgetUnavailableError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="金额预算状态暂不可用",
                ) from exc

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
            (
                "Starting evaluation execution: kb_id=%s eval_version=%s "
                "dataset_count=%s rag_concurrency=%s"
            ),
            kb_id,
            eval_version,
            len(datasets),
            self.rag_concurrency,
        )

        execution_started_at = time.perf_counter()
        evaluated_at = shanghai_now_naive()
        rag_phases_by_dataset: dict[int, _RagPhase] = {}
        ragas_dataset_ids: list[int] = []
        ragas_tasks: list[asyncio.Task[RagasEvaluationResult]] = []

        rag_semaphore = asyncio.Semaphore(self.rag_concurrency)

        async def execute_dataset(dataset: EvalDataset) -> _RagPhase:
            async with rag_semaphore:
                phase = await self._run_rag_phase(
                    kb_id=kb_id,
                    eval_version=eval_version,
                    dataset=dataset,
                    user=user,
                    rag_executor=rag_executor,
                    evaluated_at=evaluated_at,
                )
            rag_phases_by_dataset[dataset.id] = phase
            if phase.sample is not None:
                ragas_dataset_ids.append(dataset.id)
                ragas_tasks.append(
                    asyncio.create_task(
                        self._evaluate_ragas(
                            ragas_evaluator=ragas_evaluator,
                            sample=phase.sample,
                        )
                    )
                )
            return phase

        await asyncio.gather(*(execute_dataset(dataset) for dataset in datasets))

        # RAGAS 不使用请求数据库会话；每题 RAG 完成后立即启动评分，
        # 让评分与仍在执行的其他题目 RAG 重叠。
        ragas_results = await asyncio.gather(*ragas_tasks, return_exceptions=True)
        ragas_phases_by_dataset = dict(zip(ragas_dataset_ids, ragas_results, strict=True))

        usage = ragas_evaluator.usage
        generation_usage_tokens = sum(
            phase.usage_collector.usage_tokens
            for phase in rag_phases_by_dataset.values()
            if phase.usage_collector is not None
        )
        generation_estimated_cost_cny = sum(
            (
                phase.usage_collector.estimated_cost_cny
                for phase in rag_phases_by_dataset.values()
                if phase.usage_collector is not None
            ),
            Decimal("0"),
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if self.token_metrics is not None and usage.total_tokens > 0:
            # 判定调用的输入与输出分桶计价，与在线管道口径一致；不归属任何个人。
            token_metrics = self.token_metrics
            await token_metrics.record_usage(
                tokens=usage.llm_prompt_tokens,
                model=self.token_chat_model_name,
                token_type="input",
                kb_id=kb_id,
                user_scoped=False,
            )
            await token_metrics.record_usage(
                tokens=usage.llm_completion_tokens,
                model=self.token_chat_model_name,
                token_type="evaluation",
                kb_id=kb_id,
                user_scoped=False,
            )
            await token_metrics.record_usage(
                tokens=usage.embedding_tokens,
                model=self.token_embedding_model_name,
                token_type="embedding",
                kb_id=kb_id,
                user_scoped=False,
            )

        ragas_input_cost_cny = (
            self.token_metrics.estimate_cost(
                tokens=usage.llm_prompt_tokens,
                token_type="input",
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            if self.token_metrics is not None
            else Decimal("0")
        )
        ragas_evaluation_cost_cny = (
            self.token_metrics.estimate_cost(
                tokens=usage.llm_completion_tokens,
                token_type="evaluation",
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            if self.token_metrics is not None
            else Decimal("0")
        )
        ragas_embedding_cost_cny = (
            self.token_metrics.estimate_cost(
                tokens=usage.embedding_tokens,
                token_type="embedding",
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            if self.token_metrics is not None
            else Decimal("0")
        )
        run_usage = EvalRunUsage(
            kb_id=kb_id,
            eval_version=eval_version,
            generation_usage_tokens=generation_usage_tokens,
            generation_estimated_cost_cny=generation_estimated_cost_cny,
            ragas_input_tokens=usage.llm_prompt_tokens,
            ragas_evaluation_tokens=usage.llm_completion_tokens,
            ragas_embedding_tokens=usage.embedding_tokens,
            ragas_input_cost_cny=ragas_input_cost_cny,
            ragas_evaluation_cost_cny=ragas_evaluation_cost_cny,
            ragas_embedding_cost_cny=ragas_embedding_cost_cny,
        )

        results: list[EvalResult] = []
        for dataset in datasets:
            phase = rag_phases_by_dataset[dataset.id]
            if phase.sample is None:
                assert phase.result is not None
                results.append(phase.result)
                continue
            evaluation = ragas_phases_by_dataset[phase.dataset.id]
            results.append(
                self._build_dataset_result(
                    phase=phase,
                    generation=(
                        evaluation if isinstance(evaluation, RagasEvaluationResult) else None
                    ),
                    evaluation_error=(
                        evaluation if isinstance(evaluation, BaseException) else None
                    ),
                )
            )

        # 评估耗时表示整轮真实执行窗口；保存和报告聚合属于持久化收尾，不计入该指标。
        duration_ms = max(0, int((time.perf_counter() - execution_started_at) * 1000))
        for result in results:
            result.duration_ms = duration_ms

        try:
            await self.repository.save_results(results, run_usage=run_usage)
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

    async def _run_rag_phase(
        self,
        *,
        kb_id: int,
        eval_version: int,
        dataset: EvalDataset,
        user: CurrentUser,
        rag_executor: EvaluationRagExecutor,
        evaluated_at: datetime,
    ) -> _RagPhase:
        """执行单题 RAG，并准备可并发执行的 RAGAS 输入。"""
        usage_collector = EvaluationUsageCollector()
        try:
            execution = await rag_executor.execute(
                question=dataset.question,
                kb_ids=[kb_id],
                user=user,
                usage_collector=usage_collector,
            )
        except Exception as exc:
            logger.warning(
                "Evaluation question failed: error_type=rag_execution_failed exception_type=%s",
                type(exc).__name__,
            )
            return _RagPhase(
                eval_version=eval_version,
                dataset=dataset,
                execution=None,
                result=EvalResult(
                    dataset_id=dataset.id,
                    eval_version=eval_version,
                    hit=None,
                    rank=None,
                    actual_answer=None,
                    status=EvalResultStatus.FAILED.value,
                    error_type="rag_execution_failed",
                    eval_at=evaluated_at,
                ),
                usage_collector=usage_collector,
            )

        if execution.reranker_degraded:
            logger.info(
                "Evaluation question skipped after Reranker degradation: reason=%s",
                execution.degraded_reason,
            )
            return _RagPhase(
                eval_version=eval_version,
                dataset=dataset,
                execution=None,
                result=EvalResult(
                    dataset_id=dataset.id,
                    eval_version=eval_version,
                    hit=None,
                    rank=None,
                    actual_answer=execution.public_response.answer,
                    status=EvalResultStatus.PARTIAL.value,
                    error_type="reranker_degraded",
                    eval_at=evaluated_at,
                ),
                usage_collector=usage_collector,
            )

        actual_answer = (
            RAG_REFUSAL_ANSWER if execution.explicit_refusal else execution.public_response.answer
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

        expected_answer = dataset.expected_answer.strip() if dataset.expected_answer else ""
        sample = None
        if expected_answer and not execution.explicit_refusal:
            sample = RagasEvaluationSample(
                question=dataset.question,
                actual_answer=actual_answer,
                expected_answer=expected_answer,
                reference_contexts=execution.reference_contexts,
            )

        result = None
        if sample is None:
            result = EvalResult(
                dataset_id=dataset.id,
                eval_version=eval_version,
                hit=hit,
                rank=rank,
                actual_answer=actual_answer,
                status=EvalResultStatus.SUCCESS.value,
                eval_at=evaluated_at,
            )

        return _RagPhase(
            eval_version=eval_version,
            dataset=dataset,
            execution=execution,
            result=result,
            sample=sample,
            actual_answer=actual_answer,
            hit=hit,
            rank=rank,
            evaluated_at=evaluated_at,
            usage_collector=usage_collector,
        )

    async def _evaluate_ragas(
        self,
        *,
        ragas_evaluator: GenerationEvaluator,
        sample: RagasEvaluationSample,
    ) -> RagasEvaluationResult:
        """执行单个标准问题的四项 RAGAS 指标。"""
        return await ragas_evaluator.evaluate(sample)

    def _build_dataset_result(
        self,
        *,
        phase: _RagPhase,
        generation: RagasEvaluationResult | None,
        evaluation_error: BaseException | None,
    ) -> EvalResult:
        """把 RAG 执行结果和 RAGAS 分数合成一条评估结果。"""
        execution = phase.execution
        assert execution is not None
        assert phase.evaluated_at is not None
        faithfulness: float | None = None
        answer_relevancy: float | None = None
        context_recall: float | None = None
        context_precision: float | None = None
        result_status = EvalResultStatus.SUCCESS.value
        error_type: str | None = None

        if evaluation_error is not None:
            logger.warning(
                "RAGAS question evaluation failed: error_type=ragas_metric_failed "
                "exception_type=%s",
                type(evaluation_error).__name__,
            )
            result_status = EvalResultStatus.PARTIAL.value
            error_type = "ragas_metric_failed"
        else:
            assert generation is not None
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
                    ",".join(error.error_type.value for error in generation.errors) or "unknown",
                )

        return EvalResult(
            dataset_id=phase.dataset.id,
            eval_version=phase.eval_version,
            hit=phase.hit,
            rank=phase.rank,
            actual_answer=phase.actual_answer,
            faithfulness=faithfulness,
            answer_relevancy=answer_relevancy,
            context_recall=context_recall,
            context_precision=context_precision,
            status=result_status,
            error_type=error_type,
            eval_at=phase.evaluated_at,
        )
