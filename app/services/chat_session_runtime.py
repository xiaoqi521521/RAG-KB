from __future__ import annotations

import time
from typing import Protocol

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import CurrentUser
from app.models.kb import ChatMessage
from app.repositories.chat import ChatRepository
from app.schemas.query_cache import QueryCacheEntry
from app.schemas.rag import RagQueryResponse
from app.services.chat_sessions import ChatSessionService


class QueryResultCache(Protocol):
    """会话化问答使用的查询结果缓存边界。"""

    async def get(self, question: str, kb_ids: list[int]) -> QueryCacheEntry | None: ...

    async def put(
        self,
        question: str,
        kb_ids: list[int],
        response: RagQueryResponse,
    ) -> None: ...


class ChatSessionRuntime:
    """为不同回答输出模式提供一致的会话读写能力。"""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _get_or_create_session(
        self,
        *,
        session_id: str | None,
        kb_ids: list[int],
        user: CurrentUser,
    ) -> str:
        """使用独立数据库会话创建或刷新对话会话。"""
        async with self.session_factory() as session:
            service = ChatSessionService(ChatRepository(session))
            active_session_id = await service.get_or_create(
                session_id=session_id,
                kb_ids=kb_ids,
                user=user,
            )
            await session.commit()
            return active_session_id

    async def _save_turn(
        self,
        *,
        session_id: str,
        kb_ids: list[int] | None,
        question: str,
        answer: str,
        sources: list[dict[str, object]],
        token_count: int,
        latency_ms: int,
        user: CurrentUser,
        started_at: float | None = None,
        answer_mode: str = "knowledge_base",
        knowledge_base_searched: bool = True,
    ) -> None:
        """使用独立事务保存已完成的问答轮次。"""
        async with self.session_factory() as session:
            service = ChatSessionService(ChatRepository(session))
            saved_message = await service.save_turn(
                session_id=session_id,
                kb_ids=kb_ids,
                question=question,
                answer=answer,
                sources=sources,
                token_count=token_count,
                latency_ms=latency_ms,
                answer_mode=answer_mode,
                knowledge_base_searched=knowledge_base_searched,
                user=user,
            )
            if started_at is not None and isinstance(saved_message, ChatMessage):
                # flush 后补写已包含消息持久化阶段的耗时，再提交同一事务。
                saved_message.latency_ms = self._elapsed_ms(started_at)
            await session.commit()

    async def _load_history(self, session_id: str, user: CurrentUser) -> list[object]:
        """从完整存储中截取最近十轮，并转换为模型消息。"""
        async with self.session_factory() as session:
            service = ChatSessionService(ChatRepository(session))
            history = await service.get_history(session_id, user)

        return [
            HumanMessage(content=message.content)
            if message.role == "USER"
            else AIMessage(content=message.content)
            for message in history
        ]

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        """返回非负毫秒耗时。"""
        return max(0, int((time.perf_counter() - started_at) * 1000))
