from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.core.context import CurrentUser
from app.evaluation.service import EvaluationRunService
from app.models import EvalDataset, EvalDatasetStatus, EvalResult, EvalResultStatus
from app.repositories.chunks import ChunkSearchHit
from app.repositories.evaluations import EvaluationReport
from app.schemas.rag import RagQueryResponse
from app.services.rag_query_v4 import RagExecution


def _user() -> CurrentUser:
    return CurrentUser(user_id=7, department_id="engineering", role="USER")


def _dataset(dataset_id: int, expected_chunk_ids: list[int] | None) -> EvalDataset:
    return EvalDataset(
        id=dataset_id,
        kb_id=3,
        question=f"问题 {dataset_id}",
        expected_answer="期望答案",
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
) -> RagExecution:
    return RagExecution(
        public_response=RagQueryResponse(
            answer=answer,
            sources=[],
            hit_count=0,
            latency_ms=10,
        ),
        reranked_hits=[_hit(chunk_id) for chunk_id in chunk_ids],
        reference_contexts=[],
        prompt_context="",
        reranker_degraded=degraded,
        degraded_reason="reranker_timeout" if degraded else None,
        explicit_refusal=False,
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
    ) -> RagExecution:
        self.calls.append((question, kb_ids, user.user_id))
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeEvaluationRepository:
    def __init__(
        self,
        *,
        datasets: list[EvalDataset] | None = None,
        version_exists: bool = False,
        save_error: Exception | None = None,
    ) -> None:
        self.datasets = datasets or []
        self.version_exists_value = version_exists
        self.save_error = save_error
        self.list_calls: list[tuple[int, str | None]] = []
        self.saved_batches: list[list[EvalResult]] = []

    async def version_exists(self, *, kb_id: int, eval_version: str) -> bool:
        return self.version_exists_value

    async def list_datasets(self, *, kb_id: int, status: str | None = None):
        self.list_calls.append((kb_id, status))
        return self.datasets

    async def save_results(self, results: list[EvalResult]) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved_batches.append(results)

    async def get_report(self, *, kb_id: int, eval_version: str) -> EvaluationReport | None:
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
            eval_at=results[0].eval_at,
        )


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
        )

    assert exc_info.value.status_code == 409
    assert len(executor.calls) == 1
    assert repository.saved_batches == []
