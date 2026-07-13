from __future__ import annotations

from types import SimpleNamespace

from app.services.chat_sessions import ChatSessionService


class FakeChatRepository:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages

    async def list_messages(self, session_id: str) -> list[SimpleNamespace]:
        return self.messages


async def test_history_keeps_only_latest_five_complete_conversation_rounds() -> None:
    messages = [
        SimpleNamespace(id=index, content=f"message-{index}", role="USER") for index in range(12)
    ]
    service = ChatSessionService(FakeChatRepository(messages))  # type: ignore[arg-type]

    history = await service.get_history("session-1")

    assert [message.id for message in history] == list(range(2, 12))
