from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.services.chat_sessions import ChatSessionService


class FakeChatRepository:
    def __init__(self, messages: list[SimpleNamespace], owner_id: int = 1) -> None:
        self.messages = messages
        self.owner_id = owner_id
        self.touched: list[str] = []
        self.saved: list[str] = []

    async def get_active_session_for_user(
        self,
        session_id: str,
        user_id: int,
    ) -> SimpleNamespace | None:
        if user_id != self.owner_id:
            return None
        return SimpleNamespace(id=session_id, user_id=user_id, is_deleted=False)

    async def touch_session(self, session: SimpleNamespace) -> None:
        self.touched.append(session.id)

    async def list_messages_for_user(self, session_id: str, user_id: int) -> list[SimpleNamespace]:
        if user_id != self.owner_id:
            return []
        return self.messages

    async def add_turn_for_user(self, *, session_id: str, user_id: int, **kwargs: object) -> bool:
        if user_id != self.owner_id:
            return False
        self.saved.append(session_id)
        return True


async def test_history_keeps_only_latest_five_complete_conversation_rounds() -> None:
    messages = [
        SimpleNamespace(id=index, content=f"message-{index}", role="USER") for index in range(12)
    ]
    service = ChatSessionService(FakeChatRepository(messages))  # type: ignore[arg-type]

    history = await service.get_history("session-1", CurrentUser(1, "engineering", "MEMBER"))

    assert [message.id for message in history] == list(range(2, 12))


async def test_existing_session_rejects_a_different_user_before_touching_it() -> None:
    repository = FakeChatRepository([])
    service = ChatSessionService(repository)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await service.get_or_create(
            session_id="session-1",
            kb_ids=[2],
            user=CurrentUser(2, "sales", "MEMBER"),
        )

    assert exc_info.value.status_code == 404
    assert repository.touched == []


async def test_saving_a_turn_rejects_a_session_owned_by_another_user() -> None:
    repository = FakeChatRepository([])
    service = ChatSessionService(repository)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await service.save_turn(
            session_id="session-1",
            question="问题",
            answer="回答",
            sources=[],
            token_count=1,
            latency_ms=1,
            user=CurrentUser(2, "sales", "ADMIN"),
        )

    assert exc_info.value.status_code == 404
    assert repository.saved == []
