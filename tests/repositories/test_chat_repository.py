from __future__ import annotations

from app.repositories.chat import ChatRepository


class FakeResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return []


class FakeSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeResult()


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
