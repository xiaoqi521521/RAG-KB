from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.core.context import CurrentUser
from app.evaluation.ragas_evaluator import (
    RagasErrorType,
    RagasEvaluationResult,
    RagasEvaluationSample,
    RagasMetricError,
    RagasMetricName,
    RagasUsage,
)
from app.evaluation.service import EvaluationRunService, EvaluationUsageCollector
from app.models import EvalDataset, EvalDatasetStatus, EvalResult, EvalResultStatus
from app.repositories.chunks import ChunkSearchHit
from app.repositories.evaluations import EvaluationReport
from app.schemas.rag import RagQueryResponse
from app.services.rag_query import RAG_REFUSAL_ANSWER
from app.services.rag_query_v4 import RagExecution
from app.services.token_budget import TokenBudgetExhaustedError


def _user() -> CurrentUser:
    return CurrentUser(user_id=7, department_id="engineering", role="USER")


def _dataset(
    dataset_id: int,
    expected_chunk_ids: list[int] | None,
    *,
    expected_answer: str | None = None,
) -> EvalDataset:
    return EvalDataset(
        id=dataset_id,
        kb_id=3,
        question=f"问题 {dataset_id}",
        expected_answer=expected_answer,
        expected_chunk_ids=expected_chunk_ids,
        status=EvalDatasetStatus.ACTIVE.value,
        created_by=7,
        created_at=datetime(2026, 7, 15),
    )


def _hit(chunk_id: int) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=1,
        document_name="制度.pdf",
        kb_id=3,
        chunk_index=chunk_id,
        content="内容",
        page_num=1,
        section_title="制度",
        score=0.9,
    )


def _execution(
    chunk_ids: list[int],
    *,
    answer: str = "实际回答",
    degraded: bool = False,
    reference_contexts: list[str] | None = None,
    explicit_refusal: bool = False,
) -> RagExecution:
    return RagExecution(
        public_response=RagQueryResponse(
            answer=answer,
            sources=[],
            hit_count=0,
            latency_ms=10,
        ),
        reranked_hits=[_hit(chunk_id) for chunk_id in chunk_ids],
        reference_contexts=reference_contexts or [],
        prompt_context="",
        reranker_degraded=degraded,
        degraded_reason="reranker_timeout" if degraded else None,
        explicit_refusal=explicit_refusal,
    )


class FakeRagExecutor:
    def __init__(self, outcomes: list[RagExecution | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, list[int], int]] = []

    async def execute(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
        usage_collector: EvaluationUsageCollector,
    ) -> RagExecution:
        self.calls.append((question, kb_ids, user.user_id))
        usage_collector.add(tokens=500, cost_cny=Decimal("0.0005"))
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeRagasEvaluator:
    def __init__(
        self,
        outcomes: list[RagasEvaluationResult | Exception],
        usage: RagasUsage | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.samples: list[RagasEvaluationSample] = []
        self._usage = usage or RagasUsage()

    async def evaluate(self, sample: RagasEvaluationSample) -> RagasEvaluationResult:
        self.samples.append(sample)
        outcome = self.outcomes[len(self.samples) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def usage(self) -> RagasUsage:
        return self._usage


class FakeEvaluationRepository:
    def __init__(
        self,
        *,
        datasets: list[EvalDataset] | None = None,
        version_exists: bool = False,
        next_version: int = 1,
        save_error: Exception | None = None,
    ) -> None:
        self.datasets = datasets or []
        self.version_exists_value = version_exists
        self.next_version_value = next_version
        self.save_error = save_error
        self.list_calls: list[tuple[int, str | None]] = []
        self.next_version_calls: list[int] = []
        self.saved_batches: list[list[EvalResult]] = []

    async def version_exists(self, *, kb_id: int, eval_version: int) -> bool:
        return self.version_exists_value

    async def next_report_version(self, *, kb_id: int) -> int:
        self.next_version_calls.append(kb_id)
        return self.next_version_value

    async def list_datasets(self, *, kb_id: int, status: str | None = None):
        self.list_calls.append((kb_id, status))
        return self.datasets

    async def save_results(self, results: list[EvalResult]) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved_batches.append(results)

    async def get_report(self, *, kb_id: int, eval_version: int) -> EvaluationReport | None:
        results = self.saved_batches[0]
        samples = [result for result in results if result.hit is not None]
        hit_count = sum(result.hit is True for result in samples)
        mrr = sum(1.0 / result.rank if result.rank else 0.0 for result in samples)
        return EvaluationReport(
            kb_id=kb_id,
            eval_version=eval_version,
            total_questions=len(results),
            success_count=sum(
                result.status == EvalResultStatus.SUCCESS.value for result in results
            ),
            partial_count=sum(
                result.status == EvalResultStatus.PARTIAL.value for result in results
            ),
            failed_count=sum(result.status == EvalResultStatus.FAILED.value for result in results),
            retrieval_sample_count=len(samples),
            hit_count=hit_count,
            hit_rate_at_5=hit_count / len(samples) if samples else None,
            mrr_at_5=mrr / len(samples) if samples else None,
            faithfulness_sample_count=sum(result.faithfulness is not None for result in results),
            avg_faithfulness=None,
            answer_relevancy_sample_count=sum(
                result.answer_relevancy is not None for result in results
            ),
            avg_answer_relevancy=None,
            context_recall_sample_count=sum(result.context_recall is not None for result in results),
            avg_context_recall=None,
            context_precision_sample_count=sum(
                result.context_precision is not None for result in results
            ),
            avg_context_precision=None,
            refusal_count=sum(result.actual_answer == RAG_REFUSAL_ANSWER for result in results),
            refusal_rate=sum(result.actual_answer == RAG_REFUSAL_ANSWER for result in results)
            / len(results),
            duration_ms=results[0].duration_ms,
            usage_tokens=sum(result.usage_tokens for result in results),
            estimated_cost_cny=sum(
                (result.estimated_cost_cny for result in results), Decimal("0")
            ),
            eval_at=results[0].eval_at,
        )


@pytest.mark.asyncio
async def test_run_uses_next_report_version_when_version_is_not_provided() -> None:
    repository = FakeEvaluationRepository(datasets=[_dataset(1, [10])], next_version=7)
    executor = FakeRagExecutor([_execution([10])])
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    report = await service.run(
        kb_id=3,
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=FakeRagasEvaluator([]),
    )

    assert repository.next_version_calls == [3]
    assert repository.saved_batches[0][0].eval_version == 7
    assert report.eval_version == 7


@pytest.mark.asyncio
async def test_run_uses_active_questions_once_and_applies_null_metric_semantics() -> None:
    repository = FakeEvaluationRepository(
        datasets=[_dataset(1, [20]), _dataset(2, None), _dataset(3, [30])]
    )
    executor = FakeRagExecutor(
        [_execution([10, 20]), _execution([20]), _execution([30], degraded=True)]
    )
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    report = await service.run(
        kb_id=3,
        eval_version="release-1",
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=FakeRagasEvaluator([]),
    )

    assert repository.list_calls == [(3, EvalDatasetStatus.ACTIVE.value)]
    assert [call[1] for call in executor.calls] == [[3], [3], [3]]
    assert len(repository.saved_batches) == 1
    results = repository.saved_batches[0]
    assert (results[0].hit, results[0].rank, results[0].status) == (True, 2, "SUCCESS")
    assert (results[1].hit, results[1].rank, results[1].status) == (None, None, "SUCCESS")
    assert results[2].actual_answer == "实际回答"
    assert (results[2].hit, results[2].rank, results[2].status) == (None, None, "PARTIAL")
    assert results[2].error_type == "reranker_degraded"
    assert report.total_questions == 3
    assert report.retrieval_sample_count == 1
    assert report.hit_rate_at_5 == 1.0
    assert report.mrr_at_5 == 0.5


@pytest.mark.asyncio
async def test_run_isolates_main_flow_failure_and_all_failed_run_still_reports() -> None:
    repository = FakeEvaluationRepository(datasets=[_dataset(1, [10]), _dataset(2, [20])])
    executor = FakeRagExecutor([RuntimeError("provider body"), RuntimeError("provider body")])
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    report = await service.run(
        kb_id=3,
        eval_version="outage",
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=FakeRagasEvaluator([]),
    )

    assert len(executor.calls) == 2
    assert len(repository.saved_batches) == 1
    assert all(result.status == "FAILED" for result in repository.saved_batches[0])
    assert all(
        result.error_type == "rag_execution_failed" for result in repository.saved_batches[0]
    )
    assert report.failed_count == 2
    assert report.retrieval_sample_count == 0
    assert report.hit_rate_at_5 is None
    assert report.mrr_at_5 is None


@pytest.mark.asyncio
@pytest.mark.parametrize("version_exists,datasets", [(True, [_dataset(1, [10])]), (False, [])])
async def test_run_rejects_version_conflict_or_missing_active_questions(
    version_exists: bool,
    datasets: list[EvalDataset],
) -> None:
    repository = FakeEvaluationRepository(datasets=datasets, version_exists=version_exists)
    executor = FakeRagExecutor([])
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await service.run(
            kb_id=3,
            eval_version="release-1",
            user=_user(),
            rag_executor=executor,
            ragas_evaluator=FakeRagasEvaluator([]),
        )

    assert exc_info.value.status_code == 409
    assert executor.calls == []
    assert repository.saved_batches == []


@pytest.mark.asyncio
async def test_run_attempts_one_final_batch_and_propagates_persistence_failure() -> None:
    repository = FakeEvaluationRepository(
        datasets=[_dataset(1, [10]), _dataset(2, [20])],
        save_error=RuntimeError("database unavailable"),
    )
    executor = FakeRagExecutor([_execution([10]), _execution([20])])
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.run(
            kb_id=3,
            eval_version="release-1",
            user=_user(),
            rag_executor=executor,
            ragas_evaluator=FakeRagasEvaluator([]),
        )

    assert len(executor.calls) == 2
    assert repository.saved_batches == []


@pytest.mark.asyncio
async def test_run_maps_concurrent_unique_conflict_to_http_conflict() -> None:
    repository = FakeEvaluationRepository(
        datasets=[_dataset(1, [10])],
        save_error=IntegrityError("insert", {}, Exception("duplicate")),
    )
    executor = FakeRagExecutor([_execution([10])])
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await service.run(
            kb_id=3,
            eval_version="release-1",
            user=_user(),
            rag_executor=executor,
            ragas_evaluator=FakeRagasEvaluator([]),
        )

    assert exc_info.value.status_code == 409
    assert len(executor.calls) == 1
    assert repository.saved_batches == []


@pytest.mark.asyncio
async def test_run_maps_actual_execution_content_to_all_ragas_scores() -> None:
    repository = FakeEvaluationRepository(
        datasets=[_dataset(1, [10], expected_answer="期望答案")]
    )
    executor = FakeRagExecutor(
        [
            _execution(
                [10],
                answer="实际回答",
                reference_contexts=["截断参考一", "截断参考二"],
            )
        ]
    )
    ragas = FakeRagasEvaluator(
        [
            RagasEvaluationResult(
                faithfulness=0.9,
                answer_relevancy=0.8,
                context_recall=0.7,
                context_precision=0.6,
                errors=(),
            )
        ]
    )
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    await service.run(
        kb_id=3,
        eval_version="generation-1",
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=ragas,
    )

    assert ragas.samples == [
        RagasEvaluationSample(
            question="问题 1",
            actual_answer="实际回答",
            expected_answer="期望答案",
            reference_contexts=["截断参考一", "截断参考二"],
        )
    ]
    result = repository.saved_batches[0][0]
    assert (
        result.faithfulness,
        result.answer_relevancy,
        result.context_recall,
        result.context_precision,
    ) == (0.9, 0.8, 0.7, 0.6)
    assert result.status == EvalResultStatus.SUCCESS.value
    assert result.error_type is None


@pytest.mark.asyncio
async def test_run_evaluates_eligible_questions_concurrently() -> None:
    repository = FakeEvaluationRepository(
        datasets=[
            _dataset(1, [10], expected_answer="期望答案一"),
            _dataset(2, [20], expected_answer="期望答案二"),
            _dataset(3, [30], expected_answer="期望答案三"),
        ]
    )
    executor = FakeRagExecutor(
        [
            _execution([10]),
            _execution([20]),
            _execution([30]),
        ]
    )

    class ConcurrentRagasEvaluator:
        def __init__(self) -> None:
            self.samples: list[RagasEvaluationSample] = []
            self.active_count = 0
            self.max_active_count = 0

        @property
        def usage(self) -> RagasUsage:
            return RagasUsage()

        async def evaluate(self, sample: RagasEvaluationSample) -> RagasEvaluationResult:
            self.samples.append(sample)
            self.active_count += 1
            self.max_active_count = max(self.max_active_count, self.active_count)
            await asyncio.sleep(0)
            self.active_count -= 1
            return RagasEvaluationResult(
                faithfulness=0.9,
                answer_relevancy=0.8,
                context_recall=0.7,
                context_precision=0.6,
                errors=(),
            )

    ragas = ConcurrentRagasEvaluator()
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    report = await service.run(
        kb_id=3,
        eval_version="concurrent-generation",
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=ragas,  # type: ignore[arg-type]
    )

    assert ragas.max_active_count == 3
    assert [sample.question for sample in ragas.samples] == ["问题 1", "问题 2", "问题 3"]
    results = repository.saved_batches[0]
    assert [result.dataset_id for result in results] == [1, 2, 3]
    assert report.eval_version == "concurrent-generation"


@pytest.mark.asyncio
async def test_run_limits_concurrent_rag_execution_and_keeps_result_order() -> None:
    repository = FakeEvaluationRepository(
        datasets=[
            _dataset(1, [10]),
            _dataset(2, [20]),
            _dataset(3, [30]),
        ]
    )

    class BoundedRagExecutor:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.active_count = 0
            self.max_active_count = 0

        async def execute(
            self,
            *,
            question: str,
            kb_ids: list[int],
            user: CurrentUser,
            usage_collector: EvaluationUsageCollector,
        ):
            self.calls.append(question)
            self.active_count += 1
            self.max_active_count = max(self.max_active_count, self.active_count)
            dataset_id = int(question.rsplit(" ", 1)[-1])
            await asyncio.sleep((3 - dataset_id) * 0.01)
            self.active_count -= 1
            return _execution([dataset_id * 10])

    executor = BoundedRagExecutor()
    service = EvaluationRunService(
        repository=repository,  # type: ignore[arg-type]
        rag_concurrency=2,
    )

    report = await service.run(
        kb_id=3,
        eval_version="bounded-rag",
        user=_user(),
        rag_executor=executor,  # type: ignore[arg-type]
        ragas_evaluator=FakeRagasEvaluator([]),
    )

    assert executor.max_active_count == 2
    assert len(executor.calls) == 3
    assert [result.dataset_id for result in repository.saved_batches[0]] == [1, 2, 3]
    assert report.eval_version == "bounded-rag"


@pytest.mark.asyncio
async def test_run_stores_one_duration_for_all_questions_in_version() -> None:
    repository = FakeEvaluationRepository(datasets=[_dataset(1, [10], expected_answer="期望答案")])
    executor = FakeRagExecutor([_execution([10])])
    ragas = FakeRagasEvaluator(
        [
            RagasEvaluationResult(
                faithfulness=0.9,
                answer_relevancy=0.8,
                context_recall=0.7,
                context_precision=0.6,
                errors=(),
            )
        ]
    )
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    report = await service.run(
        kb_id=3,
        eval_version="duration-1",
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=ragas,
    )

    [result] = repository.saved_batches[0]
    assert result.duration_ms is not None
    assert result.duration_ms >= 0
    assert report.duration_ms == result.duration_ms


@pytest.mark.asyncio
async def test_run_skips_ragas_for_missing_answer_refusal_and_reranker_degradation() -> None:
    repository = FakeEvaluationRepository(
        datasets=[
            _dataset(1, [10]),
            _dataset(2, [20], expected_answer="期望答案"),
            _dataset(3, [30], expected_answer="期望答案"),
        ]
    )
    executor = FakeRagExecutor(
        [
            _execution([10]),
            _execution([20], answer="模型拒答", explicit_refusal=True),
            _execution([30], degraded=True),
        ]
    )
    ragas = FakeRagasEvaluator([])
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    await service.run(
        kb_id=3,
        eval_version="skip-generation",
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=ragas,
    )

    assert ragas.samples == []
    missing_answer, refusal, degraded = repository.saved_batches[0]
    assert missing_answer.status == EvalResultStatus.SUCCESS.value
    assert refusal.actual_answer == RAG_REFUSAL_ANSWER
    assert refusal.status == EvalResultStatus.SUCCESS.value
    assert degraded.status == EvalResultStatus.PARTIAL.value
    for result in (missing_answer, refusal, degraded):
        assert result.faithfulness is None
        assert result.answer_relevancy is None
        assert result.context_recall is None
        assert result.context_precision is None


@pytest.mark.asyncio
async def test_run_keeps_valid_scores_when_one_ragas_metric_fails(caplog) -> None:
    caplog.set_level(logging.INFO)
    repository = FakeEvaluationRepository(
        datasets=[_dataset(1, [10], expected_answer="敏感期望答案")]
    )
    executor = FakeRagExecutor(
        [_execution([10], answer="敏感实际回答", reference_contexts=["敏感参考正文"])]
    )
    ragas = FakeRagasEvaluator(
        [
            RagasEvaluationResult(
                faithfulness=0.9,
                answer_relevancy=0.8,
                context_recall=None,
                context_precision=0.6,
                errors=(
                    RagasMetricError(
                        metric=RagasMetricName.CONTEXT_RECALL,
                        error_type=RagasErrorType.TIMEOUT,
                    ),
                ),
            )
        ]
    )
    service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    await service.run(
        kb_id=3,
        eval_version="partial-generation",
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=ragas,
    )

    result = repository.saved_batches[0][0]
    assert (result.faithfulness, result.answer_relevancy, result.context_precision) == (
        0.9,
        0.8,
        0.6,
    )
    assert result.context_recall is None
    assert result.status == EvalResultStatus.PARTIAL.value
    assert result.error_type == "ragas_metric_failed"
    assert "context_recall" in caplog.text
    assert "timeout" in caplog.text
    assert "敏感期望答案" not in caplog.text
    assert "敏感实际回答" not in caplog.text
    assert "敏感参考正文" not in caplog.text


class RecordingTokenMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str, int, bool]] = []

    async def record_usage(
        self,
        *,
        tokens: int,
        model: str,
        token_type: str,
        kb_id: int,
        user_scoped: bool = True,
    ) -> None:
        self.calls.append((tokens, model, token_type, kb_id, user_scoped))

    def estimate_cost(self, *, tokens: int, token_type: str) -> Decimal:
        prices = {
            "input": Decimal("0.001"),
            "evaluation": Decimal("0.002"),
            "embedding": Decimal("0.0005"),
        }
        if tokens <= 0:
            return Decimal("0")
        return Decimal(tokens) / Decimal("1000") * prices[token_type]


@pytest.mark.asyncio
async def test_run_records_ragas_usage_without_personal_attribution() -> None:
    repository = FakeEvaluationRepository(
        datasets=[_dataset(1, [10], expected_answer="期望答案")]
    )
    executor = FakeRagExecutor([_execution([10])])
    usage = RagasUsage(llm_prompt_tokens=100, llm_completion_tokens=40, embedding_tokens=25)
    recorder = RecordingTokenMetrics()
    service = EvaluationRunService(
        repository=repository,
        token_metrics=recorder,  # type: ignore[arg-type]
        token_chat_model_name="chat-x",
        token_embedding_model_name="embed-x",
    )

    report = await service.run(
        kb_id=3,
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=FakeRagasEvaluator(
            [
                RagasEvaluationResult(
                    faithfulness=1.0,
                    answer_relevancy=1.0,
                    context_recall=1.0,
                    context_precision=1.0,
                    errors=(),
                )
            ],
            usage=usage,
        ),
    )

    assert recorder.calls == [
        (100, "chat-x", "input", 3, False),
        (40, "chat-x", "evaluation", 3, False),
        (25, "embed-x", "embedding", 3, False),
    ]
    assert repository.saved_batches[0][0].usage_tokens == 500
    assert repository.saved_batches[0][0].estimated_cost_cny == Decimal("0.0005")
    assert report.usage_tokens == 500
    # 报告费用 = 逐题生成消耗 0.0005 + 判定消耗（六位量化）0.000193。
    assert report.estimated_cost_cny == Decimal("0.000693")


class ExhaustedBudgetGate:
    async def ensure_available(self) -> None:
        raise TokenBudgetExhaustedError("今日金额预算已用尽")


@pytest.mark.asyncio
async def test_run_rejects_new_run_when_global_budget_is_exhausted() -> None:
    repository = FakeEvaluationRepository(datasets=[_dataset(1, [10])])
    executor = FakeRagExecutor([_execution([10])])
    service = EvaluationRunService(
        repository=repository,
        budget_gate=ExhaustedBudgetGate(),  # type: ignore[arg-type]
    )

    with pytest.raises(HTTPException) as error:
        await service.run(
            kb_id=3,
            user=_user(),
            rag_executor=executor,
            ragas_evaluator=FakeRagasEvaluator([]),
        )

    assert error.value.status_code == 429
    assert executor.calls == []
