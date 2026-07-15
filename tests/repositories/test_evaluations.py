from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.models import EvalResult, EvalResultStatus
from app.repositories.evaluations import EvaluationRepository


class FakeScalars:
    def all(self) -> list[Any]:
        return []


class FakeResult:
    def scalar_one(self) -> bool:
        return False

    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> FakeScalars:
        return FakeScalars()

    def all(self) -> list[Any]:
        return []


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.added_batches: list[list[Any]] = []
        self.flush_count = 0

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult()

    def add_all(self, instances: list[Any]) -> None:
        self.added_batches.append(instances)

    async def flush(self) -> None:
        self.flush_count += 1


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_dataset_lookup_scopes_by_knowledge_base_and_dataset_id() -> None:
    session = RecordingSession()
    repository = EvaluationRepository(session)  # type: ignore[arg-type]

    await repository.get_dataset(kb_id=3, dataset_id=9)

    statement = _sql(session.statements[0])
    assert "kb_eval_dataset.kb_id =" in statement
    assert "kb_eval_dataset.id =" in statement


@pytest.mark.asyncio
async def test_chunk_validation_and_summary_queries_enforce_current_published_scope() -> None:
    session = RecordingSession()
    repository = EvaluationRepository(session)  # type: ignore[arg-type]

    await repository.list_valid_chunk_ids(kb_id=3, chunk_ids=[10, 11])
    await repository.list_current_chunk_summaries(kb_id=3)

    validation_sql = _sql(session.statements[0])
    summary_sql = _sql(session.statements[1])
    for statement in (validation_sql, summary_sql):
        assert "kb_doc_chunk.kb_id =" in statement
        assert "kb_document.kb_id =" in statement
        assert "kb_doc_chunk.doc_version = kb_document.version" in statement
        assert "kb_document.status =" in statement
        assert "kb_document.is_deleted IS false" in statement
    assert "kb_doc_chunk.id IN" in validation_sql
    assert "left(kb_doc_chunk.content," in summary_sql
    assert "kb_doc_chunk.content" not in summary_sql.replace(
        "left(kb_doc_chunk.content,", ""
    )


@pytest.mark.asyncio
async def test_version_and_report_queries_are_scoped_through_dataset_knowledge_base() -> None:
    session = RecordingSession()
    repository = EvaluationRepository(session)  # type: ignore[arg-type]

    await repository.version_exists(kb_id=3, eval_version="release-1")
    await repository.list_reports(kb_id=3)

    version_sql = _sql(session.statements[0])
    report_sql = _sql(session.statements[1])
    for statement in (version_sql, report_sql):
        assert "JOIN kb_eval_dataset" in statement
        assert "kb_eval_dataset.kb_id =" in statement
    assert "kb_eval_result.eval_version =" in version_sql
    assert "GROUP BY kb_eval_result.eval_version" in report_sql
    assert "count(kb_eval_result.hit)" in report_sql
    for metric in (
        "faithfulness",
        "answer_relevancy",
        "context_recall",
        "context_precision",
    ):
        assert f"count(kb_eval_result.{metric})" in report_sql
        assert f"avg(kb_eval_result.{metric})" in report_sql
    assert "kb_eval_result.actual_answer =" in report_sql
    assert "ORDER BY max(kb_eval_result.eval_at) DESC" in report_sql


@pytest.mark.asyncio
async def test_save_results_adds_and_flushes_one_final_batch() -> None:
    session = RecordingSession()
    repository = EvaluationRepository(session)  # type: ignore[arg-type]
    results = [
        EvalResult(
            dataset_id=1,
            eval_version="release-1",
            hit=True,
            rank=1,
            actual_answer="回答",
            status=EvalResultStatus.SUCCESS.value,
        )
    ]

    await repository.save_results(results)

    assert session.added_batches == [results]
    assert session.flush_count == 1
