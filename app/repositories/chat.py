from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import shanghai_now_naive
from app.models.kb import ChatMessage, ChatMessageRole, ChatSession


class ChatRepository:
    """对话会话和消息的持久化访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_session(self, session_id: str) -> ChatSession | None:
        """按会话标识读取会话。"""
        return await self.session.get(ChatSession, session_id)

    async def add_session(self, chat_session: ChatSession) -> None:
        """加入新会话并刷新主键。"""
        self.session.add(chat_session)
        await self.session.flush()

    async def add_turn(
        self,
        *,
        session_id: str,
        question: str,
        answer: str,
        sources: list[dict[str, object]],
        token_count: int,
        latency_ms: int,
    ) -> None:
        """保存完整问答轮次，并同步更新已存在会话的统计字段。"""
        self.session.add_all(
            [
                ChatMessage(
                    session_id=session_id,
                    role=ChatMessageRole.USER.value,
                    content=question,
                ),
                ChatMessage(
                    session_id=session_id,
                    role=ChatMessageRole.ASSISTANT.value,
                    content=answer,
                    sources=sources,
                    token_count=token_count,
                    latency_ms=latency_ms,
                ),
            ]
        )

        chat_session = await self.get_session(session_id)
        if chat_session is not None:
            now = shanghai_now_naive()
            chat_session.message_count += 2
            chat_session.last_active_at = now
            if chat_session.title is None and question:
                chat_session.title = question[:50]

        await self.session.flush()

    async def touch_session(self, chat_session: ChatSession) -> None:
        """更新会话最近活跃时间。"""
        chat_session.last_active_at = shanghai_now_naive()
        await self.session.flush()

    async def list_sessions(self, user_id: int) -> list[ChatSession]:
        """按最近活跃时间倒序读取未删除会话。"""
        result = await self.session.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user_id, ChatSession.is_deleted.is_(False))
            .order_by(ChatSession.last_active_at.desc())
        )
        return list(result.scalars())

    async def list_messages(self, session_id: str) -> list[ChatMessage]:
        """按创建时间正序读取会话消息。"""
        result = await self.session.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        )
        return list(result.scalars())
