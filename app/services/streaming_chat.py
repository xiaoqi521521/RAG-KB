from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from langchain_core.messages import AIMessage, HumanMessage

from app.core.context import CurrentUser
from app.repositories.chat import ChatRepository
from app.services.chat_sessions import ChatSessionService
from app.services.rag_query_v4 import RagQueryServiceV4
from app.services.token_metrics import extract_generation_tokens, record_generation_usage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SseEvent:
    """发送给 SSE 客户端的单个命名事件。"""

    event: str
    data: str


class StreamingChatService:
    """编排 V4 RAG、会话持久化和模型流式输出。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        rag_service_factory: Callable[[AsyncSession], RagQueryServiceV4],
        timeout_seconds: float = 60,
    ) -> None:
        self.session_factory = session_factory
        self.rag_service_factory = rag_service_factory
        self.timeout_seconds = timeout_seconds

    async def stream(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
    ) -> AsyncIterator[SseEvent]:
        """执行流式问答，并将业务异常转换为客户端可处理的终态。"""
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async for event in self._stream_success(
                    question=question,
                    kb_ids=kb_ids,
                    session_id=session_id,
                    user=user,
                ):
                    yield event
        except TimeoutError:
            logger.warning("Streaming chat timed out: user_id=%s", user.user_id)
            yield SseEvent(event="error", data='{"message":"生成超时，请稍后重试"}')
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Streaming chat failed: user_id=%s", user.user_id)
            yield SseEvent(event="error", data='{"message":"请求处理失败，请稍后重试"}')

    async def _stream_success(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
    ) -> AsyncIterator[SseEvent]:
        """执行正常问答，成功后才保存完整对话轮次。"""
        started_at = time.perf_counter()
        active_session_id = await self._get_or_create_session(
            session_id=session_id,
            kb_ids=kb_ids,
            user=user,
        )
        yield SseEvent(
            event="status",
            data=(
                '{"type":"RETRIEVING","message":"正在检索知识库...","session_id":"'
                f"{active_session_id}" + '"}'
            ),
        )

        async with self.session_factory() as session:
            rag_service = self.rag_service_factory(session)
            prepared_context = await rag_service.prepare_context(
                question=question,
                kb_ids=kb_ids,
                user=user,
                started_at=started_at,
            )

        if prepared_context is None:
            yield SseEvent(event="token", data="在知识库中未找到与该问题相关的内容。")
            yield SseEvent(
                event="done",
                data=f'{{"sources":[],"latency_ms":{self._elapsed_ms(started_at)}}}',
            )
            return

        yield SseEvent(
            event="status",
            data='{"type":"GENERATING","message":"已找到相关内容，正在生成回答..."}',
        )

        messages = rag_service.build_generation_messages(
            question=question,
            prepared_context=prepared_context,
            history=await self._load_history(active_session_id),
        )
        answer_parts: list[str] = []
        full_message: Any = None
        async for chunk in rag_service.chat_model.astream(messages):
            if full_message is None:
                full_message = chunk
            else:
                full_message += chunk

            content = getattr(chunk, "content", None)
            if not isinstance(content, str) or not content:
                continue
            answer_parts.append(content)
            yield SseEvent(event="token", data=content)

        answer = "".join(answer_parts).strip()
        if not answer:
            raise RuntimeError("streaming model returned empty content")

        token_count = extract_generation_tokens(full_message) or 0
        if full_message is not None:
            await record_generation_usage(
                recorder=rag_service.token_metrics,
                response=full_message,
                pipeline="v4",
            )

        sources = rag_service.finalize_answer(
            question=question,
            answer=answer,
            prepared_context=prepared_context,
            user=user,
            kb_ids=kb_ids,
        )
        if sources is None:
            yield SseEvent(
                event="done",
                data=f'{{"sources":[],"latency_ms":{self._elapsed_ms(started_at)}}}',
            )
            return

        latency_ms = self._elapsed_ms(started_at)
        source_data = [source.model_dump(mode="json") for source in sources]
        await self._save_turn(
            session_id=active_session_id,
            question=question,
            answer=answer,
            sources=source_data,
            token_count=token_count,
            latency_ms=latency_ms,
        )
        yield SseEvent(
            event="done",
            data=(
                '{"sources":'
                f"{self._json_sources(source_data)},\"latency_ms\":{latency_ms}" + "}"
            ),
        )

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
        question: str,
        answer: str,
        sources: list[dict[str, object]],
        token_count: int,
        latency_ms: int,
    ) -> None:
        """使用独立事务保存已完成的问答轮次。"""
        async with self.session_factory() as session:
            service = ChatSessionService(ChatRepository(session))
            await service.save_turn(
                session_id=session_id,
                question=question,
                answer=answer,
                sources=sources,
                token_count=token_count,
                latency_ms=latency_ms,
            )
            await session.commit()

    async def _load_history(self, session_id: str) -> list[object]:
        """从完整存储中截取最近五轮，并转换为模型消息。"""
        async with self.session_factory() as session:
            service = ChatSessionService(ChatRepository(session))
            history = await service.get_history(session_id)

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

    @staticmethod
    def _json_sources(sources: list[dict[str, object]]) -> str:
        """把持久化来源转换为 SSE JSON 数组。"""
        import json

        return json.dumps(sources, ensure_ascii=False, separators=(",", ":"))
