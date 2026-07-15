from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app.core.context import CurrentUser
from app.evaluation.dataset_service import EvaluationDatasetService
from app.evaluation.ragas_evaluator import (
    RagasEvaluationResult,
    RagasEvaluationSample,
)
from app.evaluation.service import EvaluationRunService
from app.models import EvalDataset, EvalDatasetStatus, EvalResult, EvalResultStatus
from app.repositories.chunks import ChunkSearchHit
from app.repositories.evaluations import CurrentChunkSummary, EvaluationReport
from app.schemas.evaluation import EvalDatasetWriteRequest
from app.schemas.rag import RagQueryResponse
from app.services.enhanced_retriever import EnhancedRetrieveResult
from app.services.rag_query_v4 import RagExecution, RagQueryServiceV4
from app.services.reranker import RerankerService
from app.services.source_builder import SourceBuilder


def _user() -> CurrentUser:
    return CurrentUser(user_id=7, department_id="engineering", role="ADMIN")


def _hit(chunk_id: int, score: float = 0.1) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=5,
        document_name="制度.pdf",
        kb_id=3,
        chunk_index=chunk_id,
        content=f"制度内容 {chunk_id}",
        page_num=1,
        section_title="报销",
        score=score,
    )


class InMemoryEvaluationRepository:
    """在同一内存状态中串联数据集与正式评估服务。"""

    def __init__(self) -> None:
        self.datasets: list[EvalDataset] = []
        self.results: dict[str, list[EvalResult]] = {}
        self.next_dataset_id = 1

    async def list_current_chunk_summaries(self, *, kb_id: int):
        return [
            CurrentChunkSummary(
                chunk_id=10,
                document_id=5,
                document_name="制度.pdf",
                chunk_index=1,
                page_number=1,
                section_title="报销",
                token_count=8,
                excerpt="三十天内提交报销。",
            )
        ]

    async def list_valid_chunk_ids(self, *, kb_id: int, chunk_ids: list[int]) -> list[int]:
        return [chunk_id for chunk_id in chunk_ids if kb_id == 3 and chunk_id == 10]

    async def create_dataset(self, **values: object) -> EvalDataset:
        dataset = EvalDataset(
            id=self.next_dataset_id,
            created_at=datetime(2026, 7, 16),
            **values,
        )
        self.next_dataset_id += 1
        self.datasets.append(dataset)
        return dataset

    async def list_datasets(self, *, kb_id: int, status: str | None = None):
        return [
            dataset
            for dataset in self.datasets
            if dataset.kb_id == kb_id and (status is None or dataset.status == status)
        ]

    async def version_exists(self, *, kb_id: int, eval_version: str) -> bool:
        return eval_version in self.results

    async def save_results(self, results: list[EvalResult]) -> None:
        self.results[results[0].eval_version] = results

    async def get_report(self, *, kb_id: int, eval_version: str) -> EvaluationReport | None:
        results = self.results.get(eval_version)
        return self._report(kb_id, eval_version, results) if results else None

    async def list_reports(self, *, kb_id: int) -> list[EvaluationReport]:
        return [self._report(kb_id, version, results) for version, results in self.results.items()]

    @staticmethod
    def _report(kb_id: int, version: str, results: list[EvalResult]) -> EvaluationReport:
        retrieval = [result for result in results if result.hit is not None]
        hit_count = sum(result.hit is True for result in retrieval)

        def average(attribute: str) -> tuple[int, float | None]:
            values = [
                value
                for result in results
                if (value := getattr(result, attribute)) is not None
            ]
            return len(values), sum(values) / len(values) if values else None

        faithfulness_count, faithfulness = average("faithfulness")
        relevancy_count, relevancy = average("answer_relevancy")
        recall_count, recall = average("context_recall")
        precision_count, precision = average("context_precision")
        return EvaluationReport(
            kb_id=kb_id,
            eval_version=version,
            total_questions=len(results),
            success_count=sum(result.status == EvalResultStatus.SUCCESS.value for result in results),
            partial_count=sum(result.status == EvalResultStatus.PARTIAL.value for result in results),
            failed_count=sum(result.status == EvalResultStatus.FAILED.value for result in results),
            retrieval_sample_count=len(retrieval),
            hit_count=hit_count,
            hit_rate_at_5=hit_count / len(retrieval) if retrieval else None,
            mrr_at_5=(
                sum(1.0 / result.rank if result.rank else 0.0 for result in retrieval)
                / len(retrieval)
                if retrieval
                else None
            ),
            faithfulness_sample_count=faithfulness_count,
            avg_faithfulness=faithfulness,
            answer_relevancy_sample_count=relevancy_count,
            avg_answer_relevancy=relevancy,
            context_recall_sample_count=recall_count,
            avg_context_recall=recall,
            context_precision_sample_count=precision_count,
            avg_context_precision=precision,
            refusal_count=0,
            refusal_rate=0.0,
            eval_at=results[0].eval_at,
        )


class StaticRagExecutor:
    def __init__(self, execution: RagExecution) -> None:
        self.execution = execution
        self.calls: list[tuple[str, list[int]]] = []

    async def execute(self, *, question: str, kb_ids: list[int], user: CurrentUser):
        self.calls.append((question, kb_ids))
        return self.execution


class RecordingRagasEvaluator:
    def __init__(self) -> None:
        self.samples: list[RagasEvaluationSample] = []

    async def evaluate(self, sample: RagasEvaluationSample) -> RagasEvaluationResult:
        self.samples.append(sample)
        return RagasEvaluationResult(
            faithfulness=0.9,
            answer_relevancy=0.8,
            context_recall=0.7,
            context_precision=0.6,
            errors=(),
        )


@pytest.mark.asyncio
async def test_labeled_question_runs_once_and_returns_aggregate_history() -> None:
    repository = InMemoryEvaluationRepository()
    dataset_service = EvaluationDatasetService(repository)  # type: ignore[arg-type]
    run_service = EvaluationRunService(repository=repository)  # type: ignore[arg-type]

    chunks = await dataset_service.list_current_chunks(kb_id=3)
    dataset = await dataset_service.create_dataset(
        kb_id=3,
        request=EvalDatasetWriteRequest(
            question="报销时限？",
            expected_answer="三十天内提交。",
            expected_chunk_ids=[chunks[0].chunk_id],
        ),
        user=_user(),
    )
    executor = StaticRagExecutor(
        RagExecution(
            public_response=RagQueryResponse(
                answer="应在三十天内提交。[参考1]",
                sources=[],
                hit_count=0,
                latency_ms=5,
            ),
            reranked_hits=[_hit(10)],
            reference_contexts=["三十天内提交报销。"],
            prompt_context="[参考1] 三十天内提交报销。",
            reranker_degraded=False,
            degraded_reason=None,
            explicit_refusal=False,
        )
    )
    ragas = RecordingRagasEvaluator()

    report = await run_service.run(
        kb_id=3,
        eval_version="release-1",
        user=_user(),
        rag_executor=executor,
        ragas_evaluator=ragas,
    )
    history = await run_service.list_history(kb_id=3)

    assert dataset.status == EvalDatasetStatus.ACTIVE.value
    assert executor.calls == [("报销时限？", [3])]
    assert len(ragas.samples) == 1
    assert report.hit_rate_at_5 == 1.0
    assert report.mrr_at_5 == 1.0
    assert report.avg_faithfulness == 0.9
    assert history == [report]
    assert not hasattr(history[0], "dataset_id")


class StaticRetriever:
    def __init__(self, hits: list[ChunkSearchHit]) -> None:
        self.hits = hits

    async def retrieve(self, *, question: str, kb_ids: list[int]) -> EnhancedRetrieveResult:
        return EnhancedRetrieveResult(
            hits=self.hits,
            original_count=len(self.hits),
            hyde_count=0,
            merged_count=len(self.hits),
            degraded_reasons=(),
        )


class TimeoutRerankerClient:
    async def rerank(self, **kwargs: object):
        raise httpx.ReadTimeout("reranker timeout")


class PassThroughTrimmer:
    async def trim(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        return hits


class RejectingConfidenceFilter:
    def filter(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        raise AssertionError("降级结果不得使用 Reranker 分数阈值")


class StaticChatModel:
    def __init__(self) -> None:
        self.call_count = 0

    async def ainvoke(self, messages: list[object]) -> SimpleNamespace:
        self.call_count += 1
        return SimpleNamespace(content="应在三十天内提交。[参考1]", usage_metadata=None)


class NoopTokenRecorder:
    async def record_generation_tokens(self, *, tokens: int, source: str = "provider") -> None:
        return None


class FailIfCalledRagas:
    async def evaluate(self, sample: RagasEvaluationSample) -> RagasEvaluationResult:
        raise AssertionError("Reranker 降级时不得调用 RAGAS")


@dataclass
class RagSettings:
    rag_return_top_n: int = 2


@pytest.mark.asyncio
async def test_reranker_timeout_keeps_rrf_answer_and_skips_all_formal_metrics() -> None:
    repository = InMemoryEvaluationRepository()
    dataset_service = EvaluationDatasetService(repository)  # type: ignore[arg-type]
    await dataset_service.create_dataset(
        kb_id=3,
        request=EvalDatasetWriteRequest(
            question="报销时限？",
            expected_answer="三十天内提交。",
            expected_chunk_ids=[10],
        ),
        user=_user(),
    )
    rrf_hits = [_hit(10, 0.03), _hit(11, 0.02), _hit(12, 0.01)]
    chat_model = StaticChatModel()
    rag_service = RagQueryServiceV4(
        retriever=StaticRetriever(rrf_hits),  # type: ignore[arg-type]
        reranker=RerankerService(
            client=TimeoutRerankerClient(),  # type: ignore[arg-type]
            top_n=2,
        ),
        confidence_filter=RejectingConfidenceFilter(),  # type: ignore[arg-type]
        context_trimmer=PassThroughTrimmer(),  # type: ignore[arg-type]
        source_builder=SourceBuilder(max_context_chars=1000),
        chat_model=chat_model,
        token_metrics=NoopTokenRecorder(),  # type: ignore[arg-type]
        settings=RagSettings(),  # type: ignore[arg-type]
    )

    await EvaluationRunService(repository=repository).run(  # type: ignore[arg-type]
        kb_id=3,
        eval_version="degraded-1",
        user=_user(),
        rag_executor=rag_service,
        ragas_evaluator=FailIfCalledRagas(),
    )

    result = repository.results["degraded-1"][0]
    assert chat_model.call_count == 1
    assert result.actual_answer == "应在三十天内提交。[参考1]"
    assert result.status == EvalResultStatus.PARTIAL.value
    assert result.error_type == "reranker_degraded"
    assert result.hit is None
    assert result.rank is None
    assert result.faithfulness is None
    assert result.answer_relevancy is None
    assert result.context_recall is None
    assert result.context_precision is None
