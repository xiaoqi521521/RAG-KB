from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.context import CurrentUser
from app.core.exception_handlers import register_exception_handlers
from app.core.trace_id import register_trace_id_middleware
from app.evaluation.dataset_service import EvaluationDatasetService
from app.evaluation.ragas_evaluator import (
    RagasEvaluationResult,
    RagasEvaluationSample,
)
from app.evaluation.service import EvaluationRunService
from app.models import EvalDataset, EvalDatasetStatus, EvalResult, EvalResultStatus
from app.repositories.chunks import ChunkSearchHit
from app.repositories.evaluations import (
    CurrentChunkSummary,
    EvaluationReport,
    EvaluationRepository,
)
from app.repositories.feedback import FeedbackRepository
from app.schemas.evaluation import EvalDatasetWriteRequest
from app.schemas.rag import RagQueryResponse
from app.services.feedback import FeedbackService
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


@asynccontextmanager
async def _postgres_transaction() -> AsyncIterator[AsyncConnection]:
    """提供使用临时表且最终整体回滚的 PostgreSQL 集成事务。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        connection = await engine.connect()
    except (OSError, SQLAlchemyError) as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL test database unavailable: {type(exc).__name__}")

    transaction = await connection.begin()
    try:
        yield connection
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()


async def _create_pipeline_temp_tables(connection: AsyncConnection) -> None:
    """创建反馈候选、标注和重建流程需要的连接级临时表。"""
    statements = (
        """
            CREATE TEMP TABLE kb_chat_session (
                id VARCHAR(36) PRIMARY KEY,
                user_id BIGINT NOT NULL,
                is_deleted BOOLEAN NOT NULL DEFAULT FALSE
            ) ON COMMIT DROP
        """,
        """
            CREATE TEMP TABLE kb_chat_message (
                id BIGSERIAL PRIMARY KEY,
                session_id VARCHAR(36) NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                sources JSONB,
                token_count INT DEFAULT 0,
                latency_ms INT DEFAULT 0,
                feedback SMALLINT,
                kb_ids BIGINT[],
                answer_mode VARCHAR(30) NOT NULL DEFAULT 'knowledge_base',
                knowledge_base_searched BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT now()
            ) ON COMMIT DROP
        """,
        """
            CREATE TEMP TABLE kb_answer_feedback (
                id BIGSERIAL PRIMARY KEY,
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                feedback SMALLINT NOT NULL,
                comment TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT now(),
                UNIQUE (message_id, user_id)
            ) ON COMMIT DROP
        """,
        """
            CREATE TEMP TABLE kb_eval_dataset (
                id BIGSERIAL PRIMARY KEY,
                kb_id BIGINT NOT NULL,
                question TEXT NOT NULL,
                expected_answer TEXT,
                expected_chunk_ids BIGINT[],
                status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                review_reason VARCHAR(50),
                source_feedback_id BIGINT UNIQUE,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT now()
            ) ON COMMIT DROP
        """,
        """
            CREATE TEMP TABLE kb_eval_result (
                id BIGSERIAL PRIMARY KEY,
                dataset_id BIGINT NOT NULL
            ) ON COMMIT DROP
        """,
        """
            CREATE TEMP TABLE kb_document (
                id BIGINT PRIMARY KEY,
                kb_id BIGINT NOT NULL,
                version INT NOT NULL,
                status VARCHAR(20) NOT NULL,
                is_deleted BOOLEAN NOT NULL DEFAULT FALSE
            ) ON COMMIT DROP
        """,
        """
            CREATE TEMP TABLE kb_doc_chunk (
                id BIGINT PRIMARY KEY,
                doc_id BIGINT NOT NULL,
                kb_id BIGINT NOT NULL,
                doc_version INT NOT NULL
            ) ON COMMIT DROP
        """,
    )
    for statement in statements:
        await connection.execute(text(statement))


PROMOTE_DATASET_SQL = """
UPDATE kb_eval_dataset AS dataset
SET status = 'ACTIVE', review_reason = NULL
WHERE dataset.kb_id = :kb_id
  AND dataset.id = :dataset_id
  AND dataset.status IN ('CANDIDATE', 'NEEDS_REVIEW')
  AND btrim(dataset.question) <> ''
  AND (
      COALESCE(btrim(dataset.expected_answer), '') <> ''
      OR COALESCE(cardinality(dataset.expected_chunk_ids), 0) > 0
  )
  AND NOT EXISTS (
      SELECT 1 FROM kb_eval_result AS result
      WHERE result.dataset_id = dataset.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM unnest(
          COALESCE(dataset.expected_chunk_ids, ARRAY[]::BIGINT[])
      ) AS expected(chunk_id)
      LEFT JOIN kb_doc_chunk AS chunk
        ON chunk.id = expected.chunk_id AND chunk.kb_id = dataset.kb_id
      LEFT JOIN kb_document AS document
        ON document.id = chunk.doc_id AND document.kb_id = dataset.kb_id
      WHERE chunk.id IS NULL
         OR document.id IS NULL
         OR chunk.doc_version <> document.version
         OR document.status <> 'DONE'
         OR document.is_deleted IS TRUE
  )
RETURNING id, status
"""


@pytest.mark.asyncio
async def test_feedback_candidate_trace_reindex_and_replacement_use_postgres() -> None:
    """真实 PostgreSQL 状态流应保留追溯，并以新记录替换失效标注。"""
    async with _postgres_transaction() as connection:
        await _create_pipeline_temp_tables(connection)
        seed_statements = (
            "INSERT INTO kb_chat_session (id, user_id) VALUES ('session-1', 7)",
            """
            INSERT INTO kb_chat_message (id, session_id, role, content, created_at)
            VALUES (19, 'session-1', 'USER', '报销时限？', '2026-07-16 10:00:00')
            """,
            """
            INSERT INTO kb_chat_message
                (id, session_id, role, content, sources, kb_ids, created_at)
            VALUES
                (20, 'session-1', 'ASSISTANT', '原助手回答',
                 '[{"chunk_id": 10}]'::JSONB, ARRAY[3]::BIGINT[],
                 '2026-07-16 10:01:00')
            """,
            """
            INSERT INTO kb_document (id, kb_id, version, status)
            VALUES (5, 3, 1, 'DONE')
            """,
            """
            INSERT INTO kb_doc_chunk (id, doc_id, kb_id, doc_version)
            VALUES (10, 5, 3, 1)
            """,
        )
        for statement in seed_statements:
            await connection.execute(text(statement))
        session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
        feedback_service = FeedbackService(FeedbackRepository(session))

        from app.api.routes import feedback

        app = FastAPI()
        register_trace_id_middleware(app)
        register_exception_handlers(app)
        app.include_router(feedback.router, prefix="/api/v1/feedback")
        app.dependency_overrides[feedback.get_current_user] = _user
        app.dependency_overrides[feedback.get_feedback_service] = lambda: feedback_service
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/v1/feedback/20",
                headers={"X-Trace-Id": "feedback-flow-1"},
                json={"feedback": -1, "comment": "回答不完整"},
            )

        assert response.status_code == 200
        assert response.headers["X-Trace-Id"] == "feedback-flow-1"
        candidate = (
            await session.execute(
                text(
                    """
                    SELECT dataset.id, dataset.question, dataset.status,
                           dataset.expected_answer, dataset.expected_chunk_ids,
                           dataset.source_feedback_id, message.content, message.sources
                    FROM kb_eval_dataset AS dataset
                    JOIN kb_answer_feedback AS answer_feedback
                      ON answer_feedback.id = dataset.source_feedback_id
                    JOIN kb_chat_message AS message
                      ON message.id = answer_feedback.message_id
                    """
                )
            )
        ).one()
        candidate_id = candidate[0]
        assert candidate[1:5] == ("报销时限？", "CANDIDATE", None, None)
        assert candidate[5] is not None
        assert candidate[6] == "原助手回答"
        assert candidate[7] == [{"chunk_id": 10}]

        await session.execute(
            text(
                "UPDATE kb_eval_dataset "
                "SET expected_answer = '三十天内提交。', "
                "expected_chunk_ids = ARRAY[10]::BIGINT[] WHERE id = :dataset_id"
            ),
            {"dataset_id": candidate_id},
        )
        await session.execute(text("LOCK TABLE kb_document, kb_doc_chunk IN SHARE MODE"))
        promoted = (
            await session.execute(
                text(PROMOTE_DATASET_SQL),
                {"kb_id": 3, "dataset_id": candidate_id},
            )
        ).one()
        assert promoted == (candidate_id, "ACTIVE")

        evaluation_repository = EvaluationRepository(session)
        await evaluation_repository.invalidate_reindexed_chunk_labels(
            kb_id=3,
            old_chunk_ids=[10],
        )
        invalidated_status = (
            await session.execute(
                text("SELECT status FROM kb_eval_dataset WHERE id = :dataset_id"),
                {"dataset_id": candidate_id},
            )
        ).scalar_one()
        assert invalidated_status == EvalDatasetStatus.NEEDS_REVIEW.value

        await session.execute(
            text("INSERT INTO kb_eval_result (dataset_id) VALUES (:dataset_id)"),
            {"dataset_id": candidate_id},
        )
        await session.execute(text("UPDATE kb_document SET version = 2 WHERE id = 5"))
        await session.execute(
            text(
                "INSERT INTO kb_doc_chunk (id, doc_id, kb_id, doc_version) "
                "VALUES (20, 5, 3, 2)"
            )
        )
        dataset_service = EvaluationDatasetService(evaluation_repository)
        archived = await dataset_service.archive_dataset(kb_id=3, dataset_id=candidate_id)
        replacement = await dataset_service.create_dataset(
            kb_id=3,
            request=EvalDatasetWriteRequest(
                question="报销时限？",
                expected_answer="三十天内提交。",
                expected_chunk_ids=[20],
            ),
            user=_user(),
        )

        assert archived.status == EvalDatasetStatus.ARCHIVED.value
        assert replacement.id != candidate_id
        assert replacement.status == EvalDatasetStatus.ACTIVE.value
        assert replacement.expected_chunk_ids == [20]
        await session.close()
