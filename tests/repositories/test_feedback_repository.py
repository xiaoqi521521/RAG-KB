from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.models import AnswerFeedback, ChatMessage, ChatMessageRole
from app.repositories.feedback import FeedbackRepository


class FakeResult:
    def __init__(self, scalar: object = None) -> None:
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar


class RecordingSession:
    def __init__(self, results: list[FakeResult] | None = None) -> None:
        self.results = results or []
        self.statements: list[Any] = []
        self.flush_count = 0

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()

    async def flush(self) -> None:
        self.flush_count += 1


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_feedback_target_hard_filters_owner_session_state_and_assistant_role() -> None:
    assistant = ChatMessage(
        id=20,
        session_id="session-1",
        role=ChatMessageRole.ASSISTANT.value,
        content="回答",
        created_at=datetime(2026, 7, 15, 10, 1),
    )
    question = ChatMessage(
        id=19,
        session_id="session-1",
        role=ChatMessageRole.USER.value,
        content="最近问题",
        created_at=datetime(2026, 7, 15, 10, 0),
    )
    session = RecordingSession([FakeResult(assistant), FakeResult(question)])
    repository = FeedbackRepository(session)  # type: ignore[arg-type]

    target = await repository.get_feedback_target(message_id=20, user_id=7)

    assert target is not None
    assert target.question == "最近问题"
    ownership_sql = _sql(session.statements[0])
    question_sql = _sql(session.statements[1])
    assert "JOIN kb_chat_session" in ownership_sql
    assert "kb_chat_session.user_id =" in ownership_sql
    assert "kb_chat_session.is_deleted IS false" in ownership_sql
    assert "kb_chat_message.role =" in ownership_sql
    assert "kb_chat_message.session_id =" in question_sql
    assert "kb_chat_message.created_at <" in question_sql
    assert "kb_chat_message.id <" in question_sql
    assert "ORDER BY kb_chat_message.created_at DESC, kb_chat_message.id DESC" in question_sql
    assert "LIMIT" in question_sql


@pytest.mark.asyncio
async def test_feedback_upsert_uses_message_user_unique_key_and_returns_same_row() -> None:
    feedback = AnswerFeedback(
        id=30,
        message_id=20,
        user_id=7,
        feedback=-1,
        created_at=datetime(2026, 7, 15),
    )
    session = RecordingSession([FakeResult(feedback)])
    repository = FeedbackRepository(session)  # type: ignore[arg-type]

    returned = await repository.upsert_feedback(
        message_id=20,
        user_id=7,
        feedback=-1,
        comment="原因",
    )

    statement = _sql(session.statements[0])
    assert returned is feedback
    assert "ON CONFLICT (message_id, user_id) DO UPDATE" in statement
    assert "RETURNING kb_answer_feedback" in statement


@pytest.mark.asyncio
async def test_candidate_upsert_restores_only_archived_and_archive_updates_only_candidate() -> None:
    session = RecordingSession([FakeResult(40), FakeResult(40)])
    repository = FeedbackRepository(session)  # type: ignore[arg-type]

    await repository.create_or_restore_candidate(
        source_feedback_id=30,
        kb_id=3,
        question="问题",
        created_by=7,
    )
    await repository.archive_candidate(source_feedback_id=30)

    upsert_sql = _sql(session.statements[0])
    archive_sql = _sql(session.statements[1])
    assert "ON CONFLICT (source_feedback_id) DO UPDATE" in upsert_sql
    assert "WHERE kb_eval_dataset.status =" in upsert_sql
    assert "UPDATE kb_eval_dataset SET status=" in archive_sql
    assert "kb_eval_dataset.source_feedback_id =" in archive_sql
    assert "kb_eval_dataset.status =" in archive_sql


@pytest.mark.asyncio
async def test_reviewed_candidate_statuses_remain_unchanged() -> None:
    session = RecordingSession([FakeResult(), FakeResult()])
    repository = FeedbackRepository(session)  # type: ignore[arg-type]

    restored = await repository.create_or_restore_candidate(
        source_feedback_id=30,
        kb_id=3,
        question="问题",
        created_by=7,
    )
    archived = await repository.archive_candidate(source_feedback_id=30)

    assert restored == "unchanged"
    assert archived == "unchanged"
