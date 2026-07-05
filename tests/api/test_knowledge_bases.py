from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.models import DocumentStatus, KbDocument


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=1,
        department_id="engineering",
        role="ADMIN",
    )


class FakePermissionService:
    def __init__(self) -> None:
        self.read_checks: list[int] = []
        self.write_checks: list[int] = []

    async def require_read(self, kb_id: int, user: CurrentUser) -> None:
        self.read_checks.append(kb_id)

    async def require_write(self, kb_id: int, user: CurrentUser) -> None:
        self.write_checks.append(kb_id)


class FakeKnowledgeBaseService:
    def __init__(self) -> None:
        self.uploaded: list[tuple[int, str]] = []
        self.downloaded: list[tuple[int, int]] = []

    async def upload_document(self, kb_id: int, file, user: CurrentUser) -> KbDocument:
        self.uploaded.append((kb_id, file.filename))
        return KbDocument(
            id=7,
            kb_id=kb_id,
            file_name=file.filename,
            file_type="TXT",
            file_size=11,
            minio_path=f"kb/{kb_id}/abc-{file.filename}",
            uploaded_by=user.user_id,
            status=DocumentStatus.PENDING.value,
            version=1,
        )

    async def get_index_status(self, kb_id: int, doc_id: int):
        from app.schemas.knowledge_base import IndexStatusResponse

        return IndexStatusResponse(
            doc_id=doc_id,
            file_name="handbook.txt",
            status=DocumentStatus.DONE.value,
            error_msg=None,
            chunk_count=2,
            token_count=80,
            indexed_at=datetime(2026, 7, 4, tzinfo=UTC).replace(tzinfo=None),
            retry_count=1,
        )

    async def download_document(self, kb_id: int, doc_id: int) -> tuple[str, bytes]:
        self.downloaded.append((kb_id, doc_id))
        return "handbook.txt", b"hello"

    async def list_documents(self, kb_id: int) -> list[KbDocument]:
        return [
            KbDocument(
                id=7,
                kb_id=kb_id,
                file_name="handbook.txt",
                file_type="TXT",
                file_size=11,
                minio_path=f"kb/{kb_id}/abc-handbook.txt",
                uploaded_by=1,
                status=DocumentStatus.DONE.value,
                version=2,
                chunk_count=3,
                token_count=120,
                indexed_at=datetime(2026, 7, 4, tzinfo=UTC).replace(tzinfo=None),
            )
        ]


def _client(
    permission_service: FakePermissionService,
    kb_service: FakeKnowledgeBaseService,
) -> TestClient:
    from app.api.routes import knowledge_bases

    app = FastAPI()
    app.include_router(knowledge_bases.router, prefix="/api/v1/kb")
    app.dependency_overrides[knowledge_bases.get_permission_service] = lambda: permission_service
    app.dependency_overrides[knowledge_bases.get_knowledge_base_service] = lambda: kb_service
    app.dependency_overrides[knowledge_bases.get_current_user] = lambda: _user()
    return TestClient(app)


def test_upload_document_endpoint_returns_submitted_response() -> None:
    permission_service = FakePermissionService()
    kb_service = FakeKnowledgeBaseService()

    with _client(permission_service, kb_service) as client:
        response = client.post(
            "/api/v1/kb/10/documents",
            files={"file": ("handbook.txt", b"hello world", "text/plain")},
        )

    assert response.status_code == 202
    assert response.json()["data"]["doc_id"] == 7
    assert response.json()["data"]["status"] == "PENDING"
    assert permission_service.write_checks == [10]
    assert kb_service.uploaded == [(10, "handbook.txt")]


def test_index_status_endpoint_returns_latest_status() -> None:
    permission_service = FakePermissionService()
    kb_service = FakeKnowledgeBaseService()

    with _client(permission_service, kb_service) as client:
        response = client.get("/api/v1/kb/10/documents/7/status")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "DONE"
    assert response.json()["data"]["retry_count"] == 1
    assert permission_service.read_checks == [10]


def test_list_documents_endpoint_returns_serializable_document_items() -> None:
    permission_service = FakePermissionService()
    kb_service = FakeKnowledgeBaseService()

    with _client(permission_service, kb_service) as client:
        response = client.get("/api/v1/kb/10/documents")

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "id": 7,
            "kb_id": 10,
            "file_name": "handbook.txt",
            "file_type": "TXT",
            "file_size": 11,
            "status": "DONE",
            "error_msg": None,
            "chunk_count": 3,
            "token_count": 120,
            "version": 2,
            "uploaded_by": 1,
            "uploaded_at": None,
            "indexed_at": "2026-07-04T00:00:00",
        }
    ]
    assert permission_service.read_checks == [10]


def test_download_document_endpoint_returns_attachment_bytes() -> None:
    permission_service = FakePermissionService()
    kb_service = FakeKnowledgeBaseService()

    with _client(permission_service, kb_service) as client:
        response = client.get("/api/v1/kb/10/documents/7/download")

    assert response.status_code == 200
    assert response.content == b"hello"
    assert "attachment" in response.headers["content-disposition"]
    assert "handbook.txt" in response.headers["content-disposition"]
    assert permission_service.read_checks == [10]
    assert kb_service.downloaded == [(10, 7)]
