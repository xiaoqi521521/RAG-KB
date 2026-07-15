from __future__ import annotations

import logging
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.models import AnswerFeedback, ChatMessage, ChatMessageRole
from app.repositories.feedback import FeedbackTarget
from app.schemas.feedback import FeedbackRequest
from app.services.feedback import FeedbackService


def _user(user_id: int = 7) -> CurrentUser:
    return CurrentUser(user_id=user_id, department_id="engineering", role="USER")


def _target(*, kb_ids: list[int] | None = None, question: str | None = "用户问题") -> FeedbackTarget:
    return FeedbackTarget(
        assistant_message=ChatMessage(
            id=20,
            session_id="session-1",
            role=ChatMessageRole.ASSISTANT.value,
            content="敏感助手回答",
            kb_ids=kb_ids,
            created_at=datetime(2026, 7, 15, 10, 1),
        ),
        question=question,
    )


class FakeFeedbackRepository:
    def __init__(self, target: FeedbackTarget | None) -> None:
        self.target = target
        self.feedback = AnswerFeedback(
            id=30,
            message_id=20,
            user_id=7,
            feedback=-1,
            comment=None,
            created_at=datetime(2026, 7, 15),
        )
        self.upserts: list[dict[str, object]] = []
        self.message_values: list[int] = []
        self.candidate_upserts: list[dict[str, object]] = []
        self.candidate_archives: list[int] = []
        self.flush_count = 0

    async def get_feedback_target(self, *, message_id: int, user_id: int):
        return self.target

    async def upsert_feedback(self, **kwargs: object) -> AnswerFeedback:
        self.upserts.append(kwargs)
        self.feedback.feedback = int(kwargs["feedback"])
        self.feedback.comment = kwargs["comment"]  # type: ignore[assignment]
        return self.feedback

    def set_message_feedback(self, message: ChatMessage, feedback: int) -> None:
        message.feedback = feedback
        self.message_values.append(feedback)

    async def create_or_restore_candidate(self, **kwargs: object) -> str:
        self.candidate_upserts.append(kwargs)
        return "created_or_restored"

    async def archive_candidate(self, *, source_feedback_id: int) -> str:
        self.candidate_archives.append(source_feedback_id)
        return "archived"

    async def flush(self) -> None:
        self.flush_count += 1


@pytest.mark.asyncio
async def test_feedback_rejects_missing_foreign_or_non_assistant_target_as_not_found() -> None:
    repository = FakeFeedbackRepository(None)
    service = FeedbackService(repository)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await service.submit(
            message_id=20,
            request=FeedbackRequest(feedback=-1),
            user=_user(user_id=99),
        )

    assert exc_info.value.status_code == 404
    assert repository.upserts == []


@pytest.mark.asyncio
async def test_single_kb_downvote_upserts_feedback_message_and_candidate(caplog) -> None:
    caplog.set_level(logging.INFO)
    repository = FakeFeedbackRepository(_target(kb_ids=[3]))
    service = FeedbackService(repository)  # type: ignore[arg-type]

    feedback = await service.submit(
        message_id=20,
        request=FeedbackRequest(feedback=-1, comment="敏感反馈评论"),
        user=_user(),
    )

    assert feedback.id == 30
    assert repository.upserts == [
        {"message_id": 20, "user_id": 7, "feedback": -1, "comment": "敏感反馈评论"}
    ]
    assert repository.message_values == [-1]
    assert repository.candidate_upserts == [
        {
            "source_feedback_id": 30,
            "kb_id": 3,
            "question": "用户问题",
            "created_by": 7,
        }
    ]
    assert repository.flush_count == 1
    assert "polarity=negative" in caplog.text
    assert "candidate_action=created_or_restored" in caplog.text
    assert "敏感助手回答" not in caplog.text
    assert "敏感反馈评论" not in caplog.text
    assert "用户问题" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kb_ids,question",
    [([2, 3], "问题"), (None, "问题"), ([], "问题"), ([2], None)],
)
async def test_downvote_without_unambiguous_scope_and_question_does_not_create_candidate(
    kb_ids: list[int] | None,
    question: str | None,
) -> None:
    repository = FakeFeedbackRepository(_target(kb_ids=kb_ids, question=question))
    service = FeedbackService(repository)  # type: ignore[arg-type]

    await service.submit(
        message_id=20,
        request=FeedbackRequest(feedback=-1),
        user=_user(),
    )

    assert repository.candidate_upserts == []
    assert repository.flush_count == 1


@pytest.mark.asyncio
async def test_upvote_archives_only_repository_eligible_candidate() -> None:
    repository = FakeFeedbackRepository(_target(kb_ids=[3]))
    service = FeedbackService(repository)  # type: ignore[arg-type]

    await service.submit(
        message_id=20,
        request=FeedbackRequest(feedback=1),
        user=_user(),
    )

    assert repository.message_values == [1]
    assert repository.candidate_archives == [30]
    assert repository.candidate_upserts == []
    assert repository.flush_count == 1
