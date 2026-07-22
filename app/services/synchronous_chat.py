from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import CurrentUser
from app.schemas.rag import ChatQueryResponse
from app.services.chat_session_runtime import ChatSessionRuntime
from app.services.chat_session_runtime import QueryResultCache
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

_NO_HIT_ANSWER = "在知识库中未找到与该问题相关的内容。"


class SynchronousChatService(ChatSessionRuntime):
    """编排 V4 RAG、会话持久化和完整回答输出。"""

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

    async def query(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float | None = None,
    ) -> ChatQueryResponse:
        """执行会话化同步问答，成功后保存完整消息轮次。"""
        effective_started_at = started_at if started_at is not None else time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if self.budget_gate is None:
                    return await self._query_success(
                        question=question,
                        kb_ids=kb_ids,
                        session_id=session_id,
                        user=user,
                        started_at=effective_started_at,
                    )
                async with self.budget_gate.request_scope():
                    return await self._query_success(
                        question=question,
                        kb_ids=kb_ids,
                        session_id=session_id,
                        user=user,
                        started_at=effective_started_at,
                    )
        except TokenBudgetExhaustedError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="今日 Token 预算已用尽",
            ) from exc
        except TokenBudgetUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Token 预算状态暂不可用",
            ) from exc
        except TimeoutError as exc:
            logger.warning("Synchronous chat timed out")
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="生成超时，请稍后重试",
            ) from exc

    async def _query_success(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float,
    ) -> ChatQueryResponse:
        """执行正常问答并在回答可用时保存会话消息。"""
        active_session_id = await self._get_or_create_session(
            session_id=session_id,
            kb_ids=kb_ids,
            user=user,
        )
        history = await self._load_history(active_session_id, user)

        # 只有服务端确认会话没有任何历史时，才允许读取首轮缓存。
        if not history:
            cached = await self.query_cache.get(question, kb_ids)
            if cached is not None:
                latency_ms = self._elapsed_ms(started_at)
                source_data = [source.model_dump(mode="json") for source in cached.sources]
                await self._save_turn(
                    session_id=active_session_id,
                    kb_ids=kb_ids,
                    question=question,
                    answer=cached.answer,
                    sources=source_data,
                    token_count=0,
                    latency_ms=latency_ms,
                    user=user,
                    started_at=started_at,
                )
                latency_ms = self._elapsed_ms(started_at)
                return ChatQueryResponse(
                    session_id=active_session_id,
                    answer=cached.answer,
                    sources=cached.sources,
                    hit_count=cached.hit_count,
                    latency_ms=latency_ms,
                )

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
            return ChatQueryResponse(
                session_id=active_session_id,
                answer=_NO_HIT_ANSWER,
                sources=[],
                hit_count=0,
                latency_ms=self._elapsed_ms(started_at),
            )

        messages = rag_service.build_generation_messages(
            question=question,
            prepared_context=prepared_context,
            history=history,
        )
        response = await self._generate_answer(
            rag_service=rag_service,
            messages=messages,
            kb_ids=kb_ids,
        )
        answer = response.content.strip()
        sources = rag_service.finalize_answer(
            question=question,
            answer=answer,
            prepared_context=prepared_context,
            user=user,
            kb_ids=kb_ids,
        )
        latency_ms = self._elapsed_ms(started_at)
        if sources is None:
            return ChatQueryResponse(
                session_id=active_session_id,
                answer=answer,
                sources=[],
                hit_count=0,
                latency_ms=latency_ms,
            )

        source_data = [source.model_dump(mode="json") for source in sources]
        await self._save_turn(
            session_id=active_session_id,
            kb_ids=kb_ids,
            question=question,
            answer=answer,
            sources=source_data,
            token_count=extract_generation_tokens(response) or 0,
            latency_ms=latency_ms,
            user=user,
            started_at=started_at,
        )
        result = ChatQueryResponse(
            session_id=active_session_id,
            answer=answer,
            sources=sources,
            hit_count=len(sources),
            latency_ms=latency_ms,
        )
        if not history:
            # 消息已成功持久化后再写缓存，避免缓存出现在不完整会话旁边。
            await self.query_cache.put(question, kb_ids, result)
        return result.model_copy(update={"latency_ms": self._elapsed_ms(started_at)})

    async def _generate_answer(
        self,
        *,
        rag_service: RagQueryServiceV4,
        messages: list[object],
        kb_ids: list[int],
    ) -> Any:
        """调用模型并保持 V4 同步查询的失败语义。"""
        try:
            response = await rag_service.chat_model.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Synchronous chat generation failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="生成服务暂时不可用",
            ) from exc

        await record_generation_usage(
            recorder=rag_service.token_metrics,
            response=response,
            pipeline="v4",
            model=getattr(getattr(rag_service, "settings", None), "chat_model", "unknown"),
            kb_id=knowledge_base_scope(kb_ids),
        )
        content = getattr(response, "content", None)
        if not isinstance(content, str) or not content.strip():
            logger.warning("Synchronous chat generation returned empty content")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="生成服务暂时不可用",
            )
        return response

    async def _ensure_budget(self) -> None:
        """在缓存未命中后检查预算，避免缓存回答占用闸门。"""
        if self.budget_gate is not None:
            await self.budget_gate.ensure_available()
