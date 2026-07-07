from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.models import DocumentStatus
from app.schemas.knowledge_base import DocumentReindexSubmitResponse


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=1,
        department_id="engineering",
        role="ADMIN",
    )


class FakePermissionService:
    def __init__(self) -> None:
        self.write_checks: list[int] = []

    async def require_write(self, kb_id: int, user: CurrentUser) -> None:
        self.write_checks.append(kb_id)


class FakeDocumentUpdateService:
    def __init__(self) -> None:
        self.replaced: list[tuple[int, int, str]] = []
        self.force_reindexed: list[tuple[int, int]] = []

    async def replace_content(self, kb_id: int, doc_id: int, file, user: CurrentUser):
        self.replaced.append((kb_id, doc_id, file.filename))
        return DocumentReindexSubmitResponse(
            doc_id=doc_id,
            file_name=file.filename,
            status=DocumentStatus.DONE.value,
            task_id=30,
            message="文档替换任务已提交，新版本索引完成前继续使用当前可查询版本",
        )

    async def force_reindex(self, kb_id: int, doc_id: int):
        self.force_reindexed.append((kb_id, doc_id))
        return DocumentReindexSubmitResponse(
            doc_id=doc_id,
            file_name="handbook.txt",
            status=DocumentStatus.DONE.value,
            task_id=31,
            message="强制重建索引任务已提交，已发布版本在重建期间继续可查询",
        )


def _client(
    permission_service: FakePermissionService,
    update_service: FakeDocumentUpdateService,
) -> TestClient:
    from app.api.routes import document_updates

    app = FastAPI()
    app.include_router(document_updates.router, prefix="/api/v1/kb")
    app.dependency_overrides[document_updates.get_permission_service] = lambda: permission_service
    app.dependency_overrides[document_updates.get_document_update_service] = lambda: update_service
    app.dependency_overrides[document_updates.get_current_user] = lambda: _user()
    return TestClient(app)


def test_replace_content_endpoint_requires_write_and_returns_submitted_response() -> None:
    permission_service = FakePermissionService()
    update_service = FakeDocumentUpdateService()

    with _client(permission_service, update_service) as client:
        response = client.put(
            "/api/v1/kb/10/documents/7/content",
            files={"file": ("updated.txt", b"new text", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["data"]["task_id"] == 30
    assert response.json()["data"]["status"] == "DONE"
    assert permission_service.write_checks == [10]
    assert update_service.replaced == [(10, 7, "updated.txt")]


def test_force_reindex_endpoint_requires_write_and_returns_submitted_response() -> None:
    permission_service = FakePermissionService()
    update_service = FakeDocumentUpdateService()

    with _client(permission_service, update_service) as client:
        response = client.post("/api/v1/kb/10/documents/7/reindex-force")

    assert response.status_code == 200
    assert response.json()["data"]["task_id"] == 31
    assert response.json()["data"]["status"] == "DONE"
    assert permission_service.write_checks == [10]
    assert update_service.force_reindexed == [(10, 7)]
