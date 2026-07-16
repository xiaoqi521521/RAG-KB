from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import shanghai_now_naive
from app.models.kb import ChatMessage, ChatMessageRole, ChatSession


class ChatRepository:
    """对话会话和消息的持久化访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_session_for_user(self, session_id: str, user_id: int) -> ChatSession | None:
        """读取指定用户仍可用的对话会话。"""
        result = await self.session.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.user_id == user_id,
                ChatSession.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def add_session(self, chat_session: ChatSession) -> None:
        """加入新会话并刷新主键。"""
        self.session.add(chat_session)
        await self.session.flush()

    async def add_turn_for_user(
        self,
        *,
        session_id: str,
        user_id: int,
        kb_ids: list[int],
        question: str,
        answer: str,
        sources: list[dict[str, object]],
        token_count: int,
        latency_ms: int,
    ) -> ChatMessage | None:
        """为会话所有者保存完整问答轮次。

        Returns:
            会话归属当前用户时返回已加入会话的助手消息，否则返回 None。
        """
        chat_session = await self.get_active_session_for_user(session_id, user_id)
        if chat_session is None:
            return None

        assistant_message = ChatMessage(
            session_id=session_id,
            role=ChatMessageRole.ASSISTANT.value,
            content=answer,
            sources=sources,
            token_count=token_count,
            latency_ms=latency_ms,
            kb_ids=kb_ids,
        )
        self.session.add_all(
            [
                ChatMessage(
                    session_id=session_id,
                    role=ChatMessageRole.USER.value,
                    content=question,
                ),
                assistant_message,
            ]
        )

        now = shanghai_now_naive()
        chat_session.message_count += 2
        chat_session.last_active_at = now
        if chat_session.title is None and question:
            chat_session.title = question[:50]

        await self.session.flush()
        return assistant_message

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

    async def list_messages_for_user(self, session_id: str, user_id: int) -> list[ChatMessage]:
        """按创建时间正序读取当前用户会话的消息。"""
        result = await self.session.execute(
            select(ChatMessage)
            .join(ChatSession, ChatMessage.session_id == ChatSession.id)
            .where(
                ChatMessage.session_id == session_id,
                ChatSession.user_id == user_id,
                ChatSession.is_deleted.is_(False),
            )
            .order_by(ChatMessage.created_at.asc())
        )
        return list(result.scalars())
