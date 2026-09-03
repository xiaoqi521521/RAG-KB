from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.routes.knowledge_bases import get_permission_service
from app.api.routes.rag import (
    get_faithfulness_metrics,
    get_query_cache_service,
    get_token_budget_gate,
    get_rag_query_service,
    get_token_metrics,
)
from app.core.config import Settings, get_settings
from app.core.clients import get_chat_model
from app.core.context import CurrentUser
from app.core.database import AsyncSessionLocal
from app.core.database import get_db
from app.repositories.chat import ChatRepository
from app.schemas.common import ApiResponse
from app.schemas.rag import ChatIntent, ChatMessageResponse, ChatQueryResponse, ChatSessionResponse, RagQueryRequest
from app.services.chat_sessions import ChatSessionService
from app.services.faithfulness_evaluator import FaithfulnessMetrics
from app.services.permissions import PermissionService
from app.services.rag_query_v4 import RagQueryServiceV4
from app.services.query_cache import QueryCacheService
from app.services.streaming_chat import SseEvent, StreamingChatService
from app.services.synchronous_chat import SynchronousChatService
from app.services.token_metrics import TokenMetrics
from app.services.token_budget import GlobalTokenBudgetGate
from app.services.intent_classifier import IntentClassifier

router = APIRouter()


class StreamingChatPipeline(Protocol):
    """路由层依赖的流式问答协议。"""

    def stream(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float | None = None,
        intent: ChatIntent = ChatIntent.KNOWLEDGE_BASE_QUERY,
        rewritten_question: str | None = None,
    ) -> AsyncIterator[SseEvent]: ...


class SynchronousChatPipeline(Protocol):
    """路由层依赖的同步会话问答协议。"""

    async def query(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float | None = None,
        intent: ChatIntent = ChatIntent.KNOWLEDGE_BASE_QUERY,
        rewritten_question: str | None = None,
    ) -> ChatQueryResponse: ...


def get_streaming_chat_service(
    settings: Settings = Depends(get_settings),
    token_metrics: TokenMetrics = Depends(get_token_metrics),
    token_budget_gate: GlobalTokenBudgetGate = Depends(get_token_budget_gate),
    faithfulness_metrics: FaithfulnessMetrics = Depends(get_faithfulness_metrics),
    query_cache: QueryCacheService = Depends(get_query_cache_service),
) -> StreamingChatPipeline:
    """组装使用独立数据库会话的流式问答服务。"""

    def build_rag_service(session: AsyncSession) -> RagQueryServiceV4:
        rag_service = get_rag_query_service(
            session=session,
            settings=settings,
            token_metrics=token_metrics,
            faithfulness_metrics=faithfulness_metrics,
            permission_service=get_permission_service(session=session),
        )
        if not isinstance(rag_service, RagQueryServiceV4):
            raise RuntimeError("流式问答仅支持 rag_query_pipeline=v4")
        return rag_service

    return StreamingChatService(
        session_factory=AsyncSessionLocal,
        rag_service_factory=build_rag_service,
        query_cache=query_cache,
        timeout_seconds=settings.chat_stream_timeout_seconds,
        budget_gate=token_budget_gate,
    )


def get_synchronous_chat_service(
    settings: Settings = Depends(get_settings),
    token_metrics: TokenMetrics = Depends(get_token_metrics),
    token_budget_gate: GlobalTokenBudgetGate = Depends(get_token_budget_gate),
    faithfulness_metrics: FaithfulnessMetrics = Depends(get_faithfulness_metrics),
    query_cache: QueryCacheService = Depends(get_query_cache_service),
) -> SynchronousChatPipeline:
    """组装使用独立数据库会话的同步问答服务。"""

    def build_rag_service(session: AsyncSession) -> RagQueryServiceV4:
        rag_service = get_rag_query_service(
            session=session,
            settings=settings,
            token_metrics=token_metrics,
            faithfulness_metrics=faithfulness_metrics,
            permission_service=get_permission_service(session=session),
        )
        if not isinstance(rag_service, RagQueryServiceV4):
            raise RuntimeError("同步会话问答仅支持 rag_query_pipeline=v4")
        return rag_service

    return SynchronousChatService(
        session_factory=AsyncSessionLocal,
        rag_service_factory=build_rag_service,
        query_cache=query_cache,
        timeout_seconds=settings.chat_stream_timeout_seconds,
        budget_gate=token_budget_gate,
    )


def get_chat_session_service(
    session: AsyncSession = Depends(get_db),
) -> ChatSessionService:
    """构建请求级会话读取服务。"""
    return ChatSessionService(ChatRepository(session))


def get_intent_classifier(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> IntentClassifier:
    """构建请求级意图分类器。"""
    try:
        model = get_chat_model()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="意图识别服务暂不可用") from exc
    return IntentClassifier(
        model,
        token_metrics=getattr(request.app.state, "token_metrics", None),
        model_name=settings.chat_model,
        budget_gate=getattr(request.app.state, "token_budget_gate", None),
    )


@router.post("")
async def query_chat(
    request: RagQueryRequest,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    session_service: ChatSessionService = Depends(get_chat_session_service),
    synchronous_service: SynchronousChatPipeline = Depends(get_synchronous_chat_service),
    intent_classifier: IntentClassifier = Depends(get_intent_classifier),
) -> ApiResponse[ChatQueryResponse]:
    """执行会话化同步问答并返回完整答案。"""
    # 从权限校验前开始计时，确保响应 latency 覆盖完整同步业务路径。
    started_at = time.perf_counter()
    history = await _load_route_history(request.session_id, session_service, user)
    decision = await intent_classifier.classify_with_context(
        request.question,
        history=history,
    )
    intent = decision.intent
    normalized_kb_ids = _normalize_kb_ids(request.kb_ids)
    if intent == ChatIntent.KNOWLEDGE_BASE_QUERY:
        if not normalized_kb_ids:
            raise HTTPException(status_code=422, detail="kb_ids is required for knowledge base queries")
        for kb_id in normalized_kb_ids:
            await permission_service.require_read(kb_id, user)

    return ApiResponse.ok(
        await synchronous_service.query(
            question=request.question,
            kb_ids=normalized_kb_ids,
            session_id=request.session_id,
            user=user,
            started_at=started_at,
            intent=intent,
            rewritten_question=decision.rewritten_question,
        )
    )


@router.get("/stream", response_class=StreamingResponse)
async def stream_chat(
    question: str = Query(min_length=1, max_length=2000),
    kb_ids: list[int] = Query(default=[]),
    session_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    session_service: ChatSessionService = Depends(get_chat_session_service),
    streaming_service: StreamingChatPipeline = Depends(get_streaming_chat_service),
    intent_classifier: IntentClassifier = Depends(get_intent_classifier),
) -> StreamingResponse:
    """以 SSE 推送 V4 RAG 回答，并保留可复用的对话会话。"""
    normalized_question = question.strip()
    if not normalized_question:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="question must not be blank")

    normalized_kb_ids = _normalize_kb_ids(kb_ids)
    started_at = time.perf_counter()
    history = await _load_route_history(session_id, session_service, user)
    decision = await intent_classifier.classify_with_context(
        normalized_question,
        history=history,
    )
    intent = decision.intent
    if intent == ChatIntent.KNOWLEDGE_BASE_QUERY:
        if not normalized_kb_ids:
            raise HTTPException(status_code=422, detail="kb_ids is required for knowledge base queries")
        # 读权限必须在建立流式连接前校验，避免无权知识库内容进入模型上下文。
        for kb_id in normalized_kb_ids:
            await permission_service.require_read(kb_id, user)

    async def event_stream() -> AsyncIterator[str]:
        async for event in streaming_service.stream(
            question=normalized_question,
            kb_ids=normalized_kb_ids,
            session_id=session_id,
            user=user,
            started_at=started_at,
            intent=intent,
            rewritten_question=decision.rewritten_question,
        ):
            yield _encode_sse(event)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/sessions")
async def list_chat_sessions(
    user: CurrentUser = Depends(get_current_user),
    session_service: ChatSessionService = Depends(get_chat_session_service),
) -> ApiResponse[list[ChatSessionResponse]]:
    """按最近活跃时间倒序返回当前用户的未删除会话。"""
    sessions = await session_service.list_sessions(user)
    return ApiResponse.ok([ChatSessionResponse.model_validate(session) for session in sessions])


@router.delete("/sessions/{session_id}")
async def delete_chat_session(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
    session_service: ChatSessionService = Depends(get_chat_session_service),
) -> ApiResponse[None]:
    """删除当前用户拥有的历史会话。"""
    await session_service.delete_session(session_id, user)
    return ApiResponse.ok()


@router.get("/sessions/{session_id}/messages", response_model_exclude_defaults=True)
async def list_chat_messages(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
    session_service: ChatSessionService = Depends(get_chat_session_service),
) -> ApiResponse[list[ChatMessageResponse]]:
    """按时间正序返回当前用户拥有的会话消息。"""
    messages = await session_service.list_messages(session_id, user)
    return ApiResponse.ok([ChatMessageResponse.model_validate(message) for message in messages])


def _normalize_kb_ids(kb_ids: list[int]) -> list[int]:
    """去重知识库 ID 并拒绝非正数，保持首次出现顺序。"""
    normalized: list[int] = []
    seen: set[int] = set()
    for kb_id in kb_ids:
        if kb_id <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="kb_ids must be positive integers",
            )
        if kb_id not in seen:
            seen.add(kb_id)
            normalized.append(kb_id)
    return normalized


async def _load_route_history(session_id: str | None, service: ChatSessionService, user: CurrentUser) -> list[object]:
    """在分类前读取当前会话历史，供追问识别使用。"""
    if not session_id:
        return []
    return list(await service.list_messages(session_id, user))


def _encode_sse(event: SseEvent) -> str:
    """按 SSE 标准编码单个命名事件。"""
    lines = [f"event: {event.event}"]
    lines.extend(f"data: {line}" for line in event.data.splitlines() or [""])
    return "\n".join(lines) + "\n\n"
