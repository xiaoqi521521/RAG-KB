from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnswerFeedback,
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    EvalDataset,
    EvalDatasetStatus,
)


@dataclass(frozen=True)
class FeedbackTarget:
    """已通过会话所有权校验的助手消息及其上一条用户问题。"""

    assistant_message: ChatMessage
    question: str | None


class FeedbackRepository:
    """回答反馈及其评估候选联动的数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_feedback_target(self, *, message_id: int, user_id: int) -> FeedbackTarget | None:
        """按会话所有权和消息角色读取可反馈的助手消息。"""
        result = await self.session.execute(
            select(ChatMessage)
            .join(ChatSession, ChatMessage.session_id == ChatSession.id)
            .where(
                ChatMessage.id == message_id,
                ChatMessage.role == ChatMessageRole.ASSISTANT.value,
                ChatSession.user_id == user_id,
                ChatSession.is_deleted.is_(False),
            )
        )
        assistant_message = result.scalar_one_or_none()
        if assistant_message is None:
            return None

        # 没有显式轮次父 ID 时，以同会话内的时间和消息 ID 稳定配对最近问题。
        question_result = await self.session.execute(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == assistant_message.session_id,
                ChatMessage.role == ChatMessageRole.USER.value,
                or_(
                    ChatMessage.created_at < assistant_message.created_at,
                    and_(
                        ChatMessage.created_at == assistant_message.created_at,
                        ChatMessage.id < assistant_message.id,
                    ),
                ),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
        question_message = question_result.scalar_one_or_none()
        return FeedbackTarget(
            assistant_message=assistant_message,
            question=question_message.content if question_message is not None else None,
        )

    async def upsert_feedback(
        self,
        *,
        message_id: int,
        user_id: int,
        feedback: int,
        comment: str | None,
    ) -> AnswerFeedback:
        """按消息和用户唯一键新增或覆盖反馈。"""
        statement = insert(AnswerFeedback).values(
            message_id=message_id,
            user_id=user_id,
            feedback=feedback,
            comment=comment,
        )
        upsert_statement = statement.on_conflict_do_update(
            index_elements=[AnswerFeedback.message_id, AnswerFeedback.user_id],
            set_={
                "feedback": statement.excluded.feedback,
                "comment": statement.excluded.comment,
            },
        ).returning(AnswerFeedback)
        result = await self.session.execute(upsert_statement)
        return result.scalar_one()

    async def clear_feedback(self, *, message_id: int, user_id: int) -> AnswerFeedback | None:
        """将当前反馈置为 0，保留记录以维持差评候选的来源追溯。"""
        result = await self.session.execute(
            update(AnswerFeedback)
            .where(
                AnswerFeedback.message_id == message_id,
                AnswerFeedback.user_id == user_id,
                AnswerFeedback.feedback.in_([-1, 1]),
            )
            .values(feedback=0, comment=None)
            .returning(AnswerFeedback)
        )
        return result.scalar_one_or_none()

    def set_message_feedback(self, message: ChatMessage, feedback: int | None) -> None:
        """同步助手消息上的快捷反馈值。"""
        message.feedback = feedback

    async def create_or_restore_candidate(
        self,
        *,
        source_feedback_id: int,
        kb_id: int,
        question: str,
        created_by: int,
    ) -> str:
        """为单知识库差评创建候选，或仅恢复已归档候选。"""
        statement = insert(EvalDataset).values(
            kb_id=kb_id,
            question=question,
            expected_answer=None,
            expected_chunk_ids=None,
            status=EvalDatasetStatus.CANDIDATE.value,
            review_reason=None,
            source_feedback_id=source_feedback_id,
            created_by=created_by,
        )
        upsert_statement = statement.on_conflict_do_update(
            index_elements=[EvalDataset.source_feedback_id],
            index_where=EvalDataset.source_feedback_id.is_not(None),
            set_={
                "status": EvalDatasetStatus.CANDIDATE.value,
                "review_reason": None,
            },
            where=EvalDataset.status == EvalDatasetStatus.ARCHIVED.value,
        ).returning(EvalDataset.id)
        result = await self.session.execute(upsert_statement)
        return "created_or_restored" if result.scalar_one_or_none() is not None else "unchanged"

    async def archive_candidate(self, *, source_feedback_id: int) -> str:
        """点赞时仅归档尚未审核的候选。"""
        result = await self.session.execute(
            update(EvalDataset)
            .where(
                EvalDataset.source_feedback_id == source_feedback_id,
                EvalDataset.status == EvalDatasetStatus.CANDIDATE.value,
            )
            .values(status=EvalDatasetStatus.ARCHIVED.value)
            .returning(EvalDataset.id)
        )
        return "archived" if result.scalar_one_or_none() is not None else "unchanged"

    async def flush(self) -> None:
        """刷新当前反馈事务中的 ORM 状态。"""
        await self.session.flush()
