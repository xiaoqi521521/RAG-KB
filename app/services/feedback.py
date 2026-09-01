from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app.core.context import CurrentUser
from app.models import AnswerFeedback
from app.repositories.feedback import FeedbackRepository
from app.schemas.feedback import FeedbackRequest

logger = logging.getLogger(__name__)


class FeedbackService:
    """保存回答反馈，并联动单知识库差评候选。"""

    def __init__(self, repository: FeedbackRepository) -> None:
        self.repository = repository

    async def submit(
        self,
        *,
        message_id: int,
        request: FeedbackRequest,
        user: CurrentUser,
    ) -> AnswerFeedback:
        """覆盖当前用户反馈，并在同一事务内维护候选状态。"""
        if request.feedback is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="feedback must be -1 or 1 when submitting",
            )

        target = await self.repository.get_feedback_target(
            message_id=message_id,
            user_id=user.user_id,
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="回答消息不存在",
            )

        feedback = await self.repository.upsert_feedback(
            message_id=message_id,
            user_id=user.user_id,
            feedback=request.feedback,
            comment=request.comment,
        )
        self.repository.set_message_feedback(target.assistant_message, request.feedback)

        candidate_action = "skipped_scope"
        if request.feedback == -1:
            kb_ids = target.assistant_message.kb_ids
            if kb_ids is not None and len(kb_ids) == 1 and target.question is not None:
                candidate_action = await self.repository.create_or_restore_candidate(
                    source_feedback_id=feedback.id,
                    kb_id=kb_ids[0],
                    question=target.question,
                    created_by=user.user_id,
                )
        else:
            candidate_action = await self.repository.archive_candidate(
                source_feedback_id=feedback.id
            )

        await self.repository.flush()
        polarity = "negative" if request.feedback == -1 else "positive"
        logger.info(
            "answer_feedback_saved=true polarity=%s candidate_action=%s",
            polarity,
            candidate_action,
        )
        return feedback

    async def remove(self, *, message_id: int, user: CurrentUser) -> None:
        """取消当前用户反馈，并归档尚未审核的差评候选。"""
        target = await self.repository.get_feedback_target(
            message_id=message_id,
            user_id=user.user_id,
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="回答消息不存在",
            )

        feedback = await self.repository.clear_feedback(
            message_id=message_id,
            user_id=user.user_id,
        )
        self.repository.set_message_feedback(target.assistant_message, None)

        candidate_action = "unchanged"
        if feedback is not None:
            # clear_feedback 返回的是已更新为 0 的记录；候选归档本身只影响 CANDIDATE。
            candidate_action = await self.repository.archive_candidate(
                source_feedback_id=feedback.id
            )

        await self.repository.flush()
        logger.info(
            "answer_feedback_removed=true candidate_action=%s",
            candidate_action,
        )
