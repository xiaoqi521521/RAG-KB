from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.context import CurrentUser
from app.core.database import get_db
from app.repositories.feedback import FeedbackRepository
from app.schemas.common import ApiResponse
from app.schemas.feedback import FeedbackRequest, FeedbackResponse
from app.services.feedback import FeedbackService

router = APIRouter()


def get_feedback_service(session: AsyncSession = Depends(get_db)) -> FeedbackService:
    """构建回答反馈服务及其数据库仓储。"""
    return FeedbackService(FeedbackRepository(session))


@router.post("/{message_id}")
async def submit_feedback(
    message_id: int,
    request: FeedbackRequest,
    user: CurrentUser = Depends(get_current_user),
    service: FeedbackService = Depends(get_feedback_service),
) -> ApiResponse[FeedbackResponse]:
    """提交、覆盖或取消当前用户对指定助手回答的反馈。"""
    if request.feedback is None:
        await service.remove(message_id=message_id, user=user)
        return ApiResponse.ok()

    feedback = await service.submit(message_id=message_id, request=request, user=user)
    return ApiResponse.ok(FeedbackResponse.model_validate(feedback))


@router.delete("/{message_id}")
async def remove_feedback(
    message_id: int,
    user: CurrentUser = Depends(get_current_user),
    service: FeedbackService = Depends(get_feedback_service),
) -> ApiResponse[None]:
    """取消当前用户对指定助手回答的反馈。"""
    await service.remove(message_id=message_id, user=user)
    return ApiResponse.ok()
