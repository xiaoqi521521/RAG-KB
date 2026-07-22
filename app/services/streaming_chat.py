from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import CurrentUser
from app.schemas.rag import RagQueryResponse
from app.services.chat_session_runtime import ChatSessionRuntime, QueryResultCache
from app.services.rag_query_v4 import RagQueryServiceV4
from app.services.token_metrics import (
    extract_generation_tokens,
    knowledge_base_scope,
    record_generation_usage,
)
from app.services.token_budget import (
    GlobalTokenBudgetGate,
    TokenBudgetExhaustedError,
    TokenBudgetUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SseEvent:
    """发送给 SSE 客户端的单个命名事件。"""

    event: str
    data: str


class StreamingChatService(ChatSessionRuntime):
    """编排 V4 RAG、会话持久化和模型流式输出。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        rag_service_factory: Callable[[AsyncSession], RagQueryServiceV4],
        query_cache: QueryResultCache,
        timeout_seconds: float = 60,
        budget_gate: GlobalTokenBudgetGate | None = None,
    ) -> None:
        super().__init__(session_factory=session_factory)
        self.rag_service_factory = rag_service_factory
        self.query_cache = query_cache
        self.timeout_seconds = timeout_seconds
        self.budget_gate = budget_gate

    async def stream(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float | None = None,
    ) -> AsyncIterator[SseEvent]:
        """执行流式问答，并将业务异常转换为客户端可处理的终态。"""
        effective_started_at = started_at if started_at is not None else time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if self.budget_gate is None:
                    async for event in self._stream_success(
                        question=question,
                        kb_ids=kb_ids,
                        session_id=session_id,
                        user=user,
                        started_at=effective_started_at,
                    ):
                        yield event
                else:
                    async with self.budget_gate.request_scope():
                        async for event in self._stream_success(
                            question=question,
                            kb_ids=kb_ids,
                            session_id=session_id,
                            user=user,
                            started_at=effective_started_at,
                        ):
                            yield event
        except TimeoutError:
            logger.warning("Streaming chat timed out")
            yield SseEvent(event="error", data='{"message":"生成超时，请稍后重试"}')
        except asyncio.CancelledError:
            raise
        except TokenBudgetExhaustedError:
            yield SseEvent(event="error", data='{"message":"今日 Token 预算已用尽"}')
        except TokenBudgetUnavailableError:
            yield SseEvent(event="error", data='{"message":"Token 预算状态暂不可用"}')
        except Exception:  # noqa: BLE001
            logger.exception("Streaming chat failed")
            yield SseEvent(event="error", data='{"message":"请求处理失败，请稍后重试"}')

    async def _stream_success(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float,
    ) -> AsyncIterator[SseEvent]:
        """执行正常问答，成功后才保存完整对话轮次。"""
        active_session_id = await self._get_or_create_session(
            session_id=session_id,
            kb_ids=kb_ids,
            user=user,
        )
        history = await self._load_history(active_session_id, user)
        yield SseEvent(
            event="status",
            data=(
                '{"type":"RETRIEVING","message":"正在检索知识库...","session_id":"'
                f"{active_session_id}" + '"}'
            ),
        )

        # 只有服务端确认会话没有任何历史时，才允许读取首轮缓存。
        if not history:
            cached = await self.query_cache.get(question, kb_ids)
            if cached is not None:
                source_data = [source.model_dump(mode="json") for source in cached.sources]
                await self._save_turn(
                    session_id=active_session_id,
                    kb_ids=kb_ids,
                    question=question,
                    answer=cached.answer,
                    sources=source_data,
                    token_count=0,
                    latency_ms=self._elapsed_ms(started_at),
                    user=user,
                    started_at=started_at,
                )
                latency_ms = self._elapsed_ms(started_at)
                yield SseEvent(event="token", data=cached.answer)
                yield SseEvent(
                    event="done",
                    data=(
                        '{"sources":'
                        f"{self._json_sources(source_data)},\"latency_ms\":{latency_ms}" + "}"
                    ),
                )
                return

        async with self.session_factory() as session:
            await self._ensure_budget()
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
            history=history,
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
                model=getattr(getattr(rag_service, "settings", None), "chat_model", "unknown"),
                kb_id=knowledge_base_scope(kb_ids),
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
            kb_ids=kb_ids,
            question=question,
            answer=answer,
            sources=source_data,
            token_count=token_count,
            latency_ms=latency_ms,
            user=user,
            started_at=started_at,
        )
        result = RagQueryResponse(
            answer=answer,
            sources=sources,
            hit_count=len(sources),
            latency_ms=latency_ms,
        )
        if not history:
            # 消息已成功持久化后再写缓存，避免缓存出现在不完整会话旁边。
            await self.query_cache.put(question, kb_ids, result)
        latency_ms = self._elapsed_ms(started_at)
        yield SseEvent(
            event="done",
            data=(
                '{"sources":'
                f"{self._json_sources(source_data)},\"latency_ms\":{latency_ms}" + "}"
            ),
        )

    @staticmethod
    def _json_sources(sources: list[dict[str, object]]) -> str:
        """把持久化来源转换为 SSE JSON 数组。"""
        import json

        return json.dumps(sources, ensure_ascii=False, separators=(",", ":"))

    async def _ensure_budget(self) -> None:
        """在缓存未命中后检查预算，缓存回答不占用模型闸门。"""
        if self.budget_gate is not None:
            await self.budget_gate.ensure_available()
