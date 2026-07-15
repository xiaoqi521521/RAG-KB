from __future__ import annotations

from datetime import datetime

from app.models import ChatMessageRole, ChatSession
from app.repositories.chat import ChatRepository


class FakeResult:
    def __init__(self, scalar: object = None) -> None:
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return []


class FakeSession:
    def __init__(self, results: list[FakeResult] | None = None) -> None:
        self.results = results or []
        self.statements: list[object] = []
        self.added: list[object] = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()

    def add_all(self, values: list[object]) -> None:
        self.added.extend(values)

    async def flush(self) -> None:
        self.flush_count += 1


async def test_owned_session_and_messages_queries_filter_by_user_and_active_status() -> None:
    session = FakeSession()
    repository = ChatRepository(session)  # type: ignore[arg-type]

    await repository.get_active_session_for_user("session-1", 7)
    await repository.list_messages_for_user("session-1", 7)

    active_session_sql = str(session.statements[0])
    messages_sql = str(session.statements[1])
    assert "kb_chat_session.user_id = :user_id_1" in active_session_sql
    assert "kb_chat_session.is_deleted IS false" in active_session_sql
    assert "kb_chat_message.session_id = :session_id_1" in messages_sql
    assert "kb_chat_session.user_id = :user_id_1" in messages_sql
    assert "kb_chat_session.is_deleted IS false" in messages_sql


async def test_saved_turn_records_scope_only_on_assistant_message() -> None:
    chat_session = ChatSession(
        id="session-1",
        user_id=7,
        kb_ids="[2, 3]",
        message_count=0,
        created_at=datetime(2026, 7, 15),
        last_active_at=datetime(2026, 7, 15),
    )
    session = FakeSession([FakeResult(chat_session)])
    repository = ChatRepository(session)  # type: ignore[arg-type]

    saved = await repository.add_turn_for_user(
        session_id="session-1",
        user_id=7,
        kb_ids=[2, 3],
        question="问题",
        answer="回答",
        sources=[],
        token_count=3,
        latency_ms=10,
    )

    assert saved is True
    assert len(session.added) == 2
    user_message, assistant_message = session.added
    assert user_message.role == ChatMessageRole.USER.value  # type: ignore[attr-defined]
    assert user_message.kb_ids is None  # type: ignore[attr-defined]
    assert assistant_message.role == ChatMessageRole.ASSISTANT.value  # type: ignore[attr-defined]
    assert assistant_message.kb_ids == [2, 3]  # type: ignore[attr-defined]
