from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.models import EvalDataset, EvalDatasetStatus
from app.repositories.evaluations import CurrentChunkSummary


def _user() -> CurrentUser:
    return CurrentUser(user_id=7, department_id="engineering", role="USER")


def _dataset() -> EvalDataset:
    return EvalDataset(
        id=9,
        kb_id=3,
        question="报销时限？",
        expected_answer="三十天内提交。",
        expected_chunk_ids=[10],
        status=EvalDatasetStatus.ACTIVE.value,
        review_reason=None,
        source_feedback_id=None,
        created_by=7,
        created_at=datetime(2026, 7, 15),
    )


class FakePermissionService:
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        self.admin_checks: list[int] = []

    async def require_admin(self, kb_id: int, user: CurrentUser) -> None:
        self.admin_checks.append(kb_id)
        if self.status_code is not None:
            raise HTTPException(status_code=self.status_code, detail="权限检查失败")


class FakeEvaluationDatasetService:
    def __init__(self) -> None:
        self.dataset = _dataset()
        self.list_calls: list[tuple[int, EvalDatasetStatus | None]] = []
        self.create_calls: list[tuple[int, object, int]] = []
        self.update_calls: list[tuple[int, int, object]] = []
        self.archive_calls: list[tuple[int, int]] = []
        self.chunk_calls: list[int] = []

    async def list_datasets(self, *, kb_id: int, status_filter: EvalDatasetStatus | None):
        self.list_calls.append((kb_id, status_filter))
        return [self.dataset]

    async def create_dataset(self, *, kb_id: int, request: object, user: CurrentUser):
        self.create_calls.append((kb_id, request, user.user_id))
        return self.dataset

    async def update_dataset(self, *, kb_id: int, dataset_id: int, request: object):
        self.update_calls.append((kb_id, dataset_id, request))
        return self.dataset

    async def archive_dataset(self, *, kb_id: int, dataset_id: int):
        self.archive_calls.append((kb_id, dataset_id))
        self.dataset.status = EvalDatasetStatus.ARCHIVED.value
        return self.dataset

    async def list_current_chunks(self, *, kb_id: int):
        self.chunk_calls.append(kb_id)
        return [
            CurrentChunkSummary(
                chunk_id=10,
                document_id=5,
                document_name="制度.pdf",
                chunk_index=2,
                page_number=3,
                section_title="报销",
                token_count=18,
                excerpt="摘要" * 100,
            )
        ]


def _client(
    permission_service: FakePermissionService,
    dataset_service: FakeEvaluationDatasetService,
) -> TestClient:
    from app.api.routes import evaluation

    app = FastAPI()
    app.include_router(evaluation.router, prefix="/api/v1/eval")
    app.dependency_overrides[evaluation.get_current_user] = lambda: _user()
    app.dependency_overrides[evaluation.get_permission_service] = lambda: permission_service
    app.dependency_overrides[evaluation.get_evaluation_dataset_service] = lambda: dataset_service
    return TestClient(app)


def test_dataset_management_and_chunk_summary_use_admin_guarded_public_api() -> None:
    permission_service = FakePermissionService()
    dataset_service = FakeEvaluationDatasetService()
    payload = {
        "question": "报销时限？",
        "expected_answer": "三十天内提交。",
        "expected_chunk_ids": [10],
    }

    with _client(permission_service, dataset_service) as client:
        listed = client.get("/api/v1/eval/3/dataset", params={"status": "ACTIVE"})
        created = client.post("/api/v1/eval/3/dataset", json=payload)
        updated = client.put("/api/v1/eval/3/dataset/9", json=payload)
        archived = client.delete("/api/v1/eval/3/dataset/9")
        chunks = client.get("/api/v1/eval/3/chunks")

    assert [response.status_code for response in (listed, created, updated, archived, chunks)] == [
        200,
        201,
        200,
        200,
        200,
    ]
    assert listed.json()["data"][0]["status"] == "ACTIVE"
    assert archived.json()["data"]["status"] == "ARCHIVED"
    chunk = chunks.json()["data"][0]
    assert chunk["chunk_id"] == 10
    assert len(chunk["excerpt"]) == 200
    assert "content" not in chunk
    assert permission_service.admin_checks == [3, 3, 3, 3, 3]
    assert dataset_service.list_calls == [(3, EvalDatasetStatus.ACTIVE)]
    assert dataset_service.chunk_calls == [3]


def test_dataset_write_rejects_client_controlled_lifecycle_fields() -> None:
    dataset_service = FakeEvaluationDatasetService()
    with _client(FakePermissionService(), dataset_service) as client:
        response = client.post(
            "/api/v1/eval/3/dataset",
            json={
                "question": "问题",
                "expected_answer": "答案",
                "status": "ACTIVE",
                "created_by": 99,
                "source_feedback_id": 88,
            },
        )

    assert response.status_code == 422
    assert dataset_service.create_calls == []


def test_permission_failure_stops_before_dataset_or_chunk_reads() -> None:
    dataset_service = FakeEvaluationDatasetService()
    permission_service = FakePermissionService(status_code=503)

    with _client(permission_service, dataset_service) as client:
        dataset_response = client.get("/api/v1/eval/3/dataset")
        chunk_response = client.get("/api/v1/eval/3/chunks")

    assert dataset_response.status_code == 503
    assert chunk_response.status_code == 503
    assert dataset_service.list_calls == []
    assert dataset_service.chunk_calls == []
