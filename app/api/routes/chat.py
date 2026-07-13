from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.routes.knowledge_bases import get_permission_service
from app.api.routes.rag import get_faithfulness_metrics, get_rag_query_service, get_token_metrics
from app.core.config import Settings, get_settings
from app.core.context import CurrentUser
from app.core.database import AsyncSessionLocal
from app.core.database import get_db
from app.repositories.chat import ChatRepository
from app.schemas.common import ApiResponse
from app.schemas.rag import ChatMessageResponse, ChatSessionResponse
from app.services.chat_sessions import ChatSessionService
from app.services.faithfulness_evaluator import FaithfulnessMetrics
from app.services.permissions import PermissionService
from app.services.rag_query_v4 import RagQueryServiceV4
from app.services.streaming_chat import SseEvent, StreamingChatService
from app.services.token_metrics import TokenMetrics

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
    ) -> AsyncIterator[SseEvent]: ...


def get_streaming_chat_service(
    settings: Settings = Depends(get_settings),
    token_metrics: TokenMetrics = Depends(get_token_metrics),
    faithfulness_metrics: FaithfulnessMetrics = Depends(get_faithfulness_metrics),
) -> StreamingChatPipeline:
    """组装使用独立数据库会话的流式问答服务。"""

    def build_rag_service(session: AsyncSession) -> RagQueryServiceV4:
        rag_service = get_rag_query_service(
            session=session,
            settings=settings,
            token_metrics=token_metrics,
            faithfulness_metrics=faithfulness_metrics,
        )
        if not isinstance(rag_service, RagQueryServiceV4):
            raise RuntimeError("流式问答仅支持 rag_query_pipeline=v4")
        return rag_service

    return StreamingChatService(
        session_factory=AsyncSessionLocal,
        rag_service_factory=build_rag_service,
        timeout_seconds=settings.chat_stream_timeout_seconds,
    )


def get_chat_session_service(
    session: AsyncSession = Depends(get_db),
) -> ChatSessionService:
    """构建请求级会话读取服务。"""
    return ChatSessionService(ChatRepository(session))


@router.get("/stream", response_class=StreamingResponse)
async def stream_chat(
    question: str = Query(min_length=1, max_length=2000),
    kb_ids: list[int] = Query(min_length=1),
    session_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    streaming_service: StreamingChatPipeline = Depends(get_streaming_chat_service),
) -> StreamingResponse:
    """以 SSE 推送 V4 RAG 回答，并保留可复用的对话会话。"""
    normalized_question = question.strip()
    if not normalized_question:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="question must not be blank")

    normalized_kb_ids = _normalize_kb_ids(kb_ids)
    # 读权限必须在建立流式连接前校验，避免无权知识库内容进入模型上下文。
    for kb_id in normalized_kb_ids:
        await permission_service.require_read(kb_id, user)

    async def event_stream() -> AsyncIterator[str]:
        async for event in streaming_service.stream(
            question=normalized_question,
            kb_ids=normalized_kb_ids,
            session_id=session_id,
            user=user,
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


@router.get("/sessions/{session_id}/messages")
async def list_chat_messages(
    session_id: str,
    session_service: ChatSessionService = Depends(get_chat_session_service),
) -> ApiResponse[list[ChatMessageResponse]]:
    """按时间正序返回会话消息，当前阶段不增加会话归属校验。"""
    messages = await session_service.list_messages(session_id)
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


def _encode_sse(event: SseEvent) -> str:
    """按 SSE 标准编码单个命名事件。"""
    lines = [f"event: {event.event}"]
    lines.extend(f"data: {line}" for line in event.data.splitlines() or [""])
    return "\n".join(lines) + "\n\n"
