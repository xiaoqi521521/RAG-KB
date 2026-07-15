from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.models import AnswerFeedback


class FakeFeedbackService:
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        self.calls: list[tuple[int, object, int]] = []

    async def submit(self, *, message_id: int, request: object, user: CurrentUser):
        self.calls.append((message_id, request, user.user_id))
        if self.status_code is not None:
            raise HTTPException(status_code=self.status_code, detail="回答消息不存在")
        return AnswerFeedback(
            id=30,
            message_id=message_id,
            user_id=user.user_id,
            feedback=-1,
            comment="原因",
            created_at=datetime(2026, 7, 15),
        )


def _client(service: FakeFeedbackService) -> TestClient:
    from app.api.routes import feedback

    app = FastAPI()
    app.include_router(feedback.router, prefix="/api/v1/feedback")
    app.dependency_overrides[feedback.get_current_user] = lambda: CurrentUser(
        7, "engineering", "USER"
    )
    app.dependency_overrides[feedback.get_feedback_service] = lambda: service
    return TestClient(app)


def test_feedback_api_accepts_only_value_and_optional_comment() -> None:
    service = FakeFeedbackService()

    with _client(service) as client:
        response = client.post(
            "/api/v1/feedback/20",
            json={"feedback": -1, "comment": " 原因 "},
        )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "message_id": 20,
        "feedback": -1,
        "comment": "原因",
        "created_at": "2026-07-15T00:00:00",
    }
    assert len(service.calls) == 1


def test_feedback_api_rejects_invalid_value_and_client_controlled_context() -> None:
    service = FakeFeedbackService()

    with _client(service) as client:
        invalid_value = client.post("/api/v1/feedback/20", json={"feedback": 0})
        controlled = client.post(
            "/api/v1/feedback/20",
            json={
                "feedback": -1,
                "user_id": 99,
                "question": "伪造问题",
                "answer": "伪造回答",
                "kb_id": 9,
            },
        )

    assert invalid_value.status_code == 422
    assert controlled.status_code == 422
    assert service.calls == []


def test_feedback_api_preserves_not_found_semantics() -> None:
    service = FakeFeedbackService(status_code=404)

    with _client(service) as client:
        response = client.post("/api/v1/feedback/20", json={"feedback": 1})

    assert response.status_code == 404
