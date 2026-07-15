from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.evaluations import EvaluationRepository


class FakeScalars:
    def all(self) -> list[Any]:
        return []


class FakeResult:
    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> FakeScalars:
        return FakeScalars()

    def all(self) -> list[Any]:
        return []


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult()


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
