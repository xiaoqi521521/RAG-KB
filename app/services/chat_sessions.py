from __future__ import annotations

import json
from uuid import uuid4

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
            chat_session = await self.repository.get_session(session_id)
            if chat_session is not None:
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
    ) -> None:
        """仅保存完整生成成功的用户问题和助手回答。"""
        await self.repository.add_turn(
            session_id=session_id,
            question=question,
            answer=answer,
            sources=sources,
            token_count=token_count,
            latency_ms=latency_ms,
        )

    async def get_history(self, session_id: str) -> list[ChatMessage]:
        """返回当前问题之前最近五轮、按时间正序的对话历史。"""
        messages = await self.repository.list_messages(session_id)
        return messages[-10:]

    async def list_sessions(self, user: CurrentUser) -> list[ChatSession]:
        """返回当前用户未删除的对话会话。"""
        return await self.repository.list_sessions(user.user_id)

    async def list_messages(self, session_id: str) -> list[ChatMessage]:
        """返回会话全部消息，供前端恢复历史记录。"""
        return await self.repository.list_messages(session_id)
