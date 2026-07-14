from __future__ import annotations

import json
from uuid import uuid4

from fastapi import HTTPException, status

from app.core.context import CurrentUser
from app.models.kb import ChatMessage, ChatSession
from app.repositories.chat import ChatRepository


class ChatSessionService:
    """管理对话会话的创建、复用和完整轮次保存。"""

    def __init__(self, repository: ChatRepository) -> None:
        self.repository = repository

    async def get_or_create(self, *, session_id: str | None, kb_ids: list[int], user: CurrentUser) -> str:
        """复用已有会话，或创建记录初始知识库范围的新会话。"""
        if session_id:
            chat_session = await self.repository.get_active_session_for_user(session_id, user.user_id)
            if chat_session is None:
                raise self._session_not_found()
            await self.repository.touch_session(chat_session)
            return session_id

        new_session_id = str(uuid4())
        await self.repository.add_session(
            ChatSession(
                id=new_session_id,
                user_id=user.user_id,
                kb_ids=json.dumps(kb_ids),
            )
        )
        return new_session_id

    async def save_turn(
        self,
        *,
        session_id: str,
        question: str,
        answer: str,
        sources: list[dict[str, object]],
        token_count: int,
        latency_ms: int,
        user: CurrentUser,
    ) -> None:
        """仅保存完整生成成功的用户问题和助手回答。"""
        saved = await self.repository.add_turn_for_user(
            session_id=session_id,
            user_id=user.user_id,
            question=question,
            answer=answer,
            sources=sources,
            token_count=token_count,
            latency_ms=latency_ms,
        )
        if not saved:
            raise self._session_not_found()

    async def get_history(self, session_id: str, user: CurrentUser) -> list[ChatMessage]:
        """返回当前问题之前最近五轮、按时间正序的对话历史。"""
        await self._require_owned_session(session_id, user)
        messages = await self.repository.list_messages_for_user(session_id, user.user_id)
        return messages[-10:]

    async def list_sessions(self, user: CurrentUser) -> list[ChatSession]:
        """返回当前用户未删除的对话会话。"""
        return await self.repository.list_sessions(user.user_id)

    async def list_messages(self, session_id: str, user: CurrentUser) -> list[ChatMessage]:
        """返回会话全部消息，供前端恢复历史记录。"""
        await self._require_owned_session(session_id, user)
        return await self.repository.list_messages_for_user(session_id, user.user_id)

    async def _require_owned_session(self, session_id: str, user: CurrentUser) -> None:
        """确认会话存在且归属于当前用户。"""
        chat_session = await self.repository.get_active_session_for_user(session_id, user.user_id)
        if chat_session is None:
            raise self._session_not_found()

    def _session_not_found(self) -> HTTPException:
        """隐藏会话是否存在，避免会话 ID 成为跨用户访问凭证。"""
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话会话不存在")
