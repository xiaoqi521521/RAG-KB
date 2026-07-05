from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.models import DocumentStatus, IndexTask, IndexTaskStatus, KbDocument, KbPermission
from app.schemas.knowledge_base import KnowledgeBaseCreateRequest
from app.services.knowledge_base import KnowledgeBaseService


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=1,
        department_id="engineering",
        role="ADMIN",
    )


@dataclass
class FakeUploadFile:
    filename: str
    size: int = 128
    content_type: str = "text/plain"


class FakeKnowledgeBaseRepository:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    async def create(
        self,
        *,
        name: str,
        description: str | None,
        department_id: str,
        is_public: bool,
        created_by: int,
    ):
        from app.models import KnowledgeBase

        kb = KnowledgeBase(
            id=10,
            name=name,
            description=description,
            department_id=department_id,
            is_public=is_public,
            created_by=created_by,
        )
        self.created.append(
            {
                "name": name,
                "description": description,
                "department_id": department_id,
                "is_public": is_public,
                "created_by": created_by,
            }
        )
        return kb


class FakePermissionRepository:
    def __init__(self) -> None:
        self.created: list[KbPermission] = []

    async def create_admin_permission(self, kb_id: int, user_id: int) -> KbPermission:
        permission = KbPermission(
            id=1,
            kb_id=kb_id,
            subject_type="USER",
            subject_id=str(user_id),
            permission="ADMIN",
            granted_by=user_id,
        )
        self.created.append(permission)
        return permission


class FakeDocumentRepository:
    def __init__(self) -> None:
        self.documents: dict[int, KbDocument] = {}
        self.created: list[dict[str, object]] = []
        self.next_id = 1

    async def create(
        self,
        *,
        kb_id: int,
        file_name: str,
        file_type: str,
        file_size: int,
        minio_path: str,
        uploaded_by: int,
    ) -> KbDocument:
        document = KbDocument(
            id=self.next_id,
            kb_id=kb_id,
            file_name=file_name,
            file_type=file_type,
            file_size=file_size,
            minio_path=minio_path,
            uploaded_by=uploaded_by,
            status=DocumentStatus.PENDING.value,
            version=1,
        )
        self.documents[document.id] = document
        self.created.append(
            {
                "kb_id": kb_id,
                "file_name": file_name,
                "file_type": file_type,
                "file_size": file_size,
                "minio_path": minio_path,
                "uploaded_by": uploaded_by,
            }
        )
        self.next_id += 1
        return document

    async def get(self, doc_id: int) -> KbDocument | None:
        return self.documents.get(doc_id)

    async def list_by_kb(self, kb_id: int) -> list[KbDocument]:
        return [doc for doc in self.documents.values() if doc.kb_id == kb_id and not doc.is_deleted]

    async def mark_deleted(self, doc_id: int) -> None:
        self.documents[doc_id].is_deleted = True

    async def reset_for_reindex(self, doc_id: int) -> None:
        doc = self.documents[doc_id]
        doc.status = DocumentStatus.PENDING.value
        doc.error_msg = None

    async def mark_failed(self, doc_id: int, error_msg: str) -> None:
        doc = self.documents[doc_id]
        doc.status = DocumentStatus.FAILED.value
        doc.error_msg = error_msg


class FakeIndexTaskRepository:
    def __init__(self) -> None:
        self.latest: dict[int, IndexTask] = {}

    async def get_latest_by_doc_id(self, doc_id: int) -> IndexTask | None:
        return self.latest.get(doc_id)


class FakeChunkRepository:
    def __init__(self) -> None:
        self.deleted_doc_ids: list[int] = []

    async def delete_by_doc_id(self, doc_id: int) -> None:
        self.deleted_doc_ids.append(doc_id)


class FakeStorageService:
    def __init__(self) -> None:
        self.uploaded: list[tuple[int, str]] = []
        self.deleted: list[str] = []
        self.downloaded: list[str] = []

    async def upload(self, kb_id: int, file: FakeUploadFile) -> str:
        self.uploaded.append((kb_id, file.filename))
        return f"kb/{kb_id}/abc-{file.filename}"

    async def download(self, object_key: str) -> bytes:
        self.downloaded.append(object_key)
        return b"file-bytes"

    async def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)


class FakeIndexService:
    def __init__(self, submit_exc: Exception | None = None) -> None:
        self.submitted: list[int] = []
        self.reindexed: list[int] = []
        self.submit_exc = submit_exc

    async def submit_index_task(self, doc_id: int) -> int:
        self.submitted.append(doc_id)
        if self.submit_exc is not None:
            raise self.submit_exc
        return 100 + doc_id

    async def reindex_document(self, doc_id: int) -> int:
        self.reindexed.append(doc_id)
        return 200 + doc_id


@dataclass
class ServiceBundle:
    service: KnowledgeBaseService
    kb_repo: FakeKnowledgeBaseRepository
    permission_repo: FakePermissionRepository
    document_repo: FakeDocumentRepository
    task_repo: FakeIndexTaskRepository
    chunk_repo: FakeChunkRepository
    storage: FakeStorageService
    index_service: FakeIndexService


def _bundle(*, submit_exc: Exception | None = None) -> ServiceBundle:
    kb_repo = FakeKnowledgeBaseRepository()
    permission_repo = FakePermissionRepository()
    document_repo = FakeDocumentRepository()
    task_repo = FakeIndexTaskRepository()
    chunk_repo = FakeChunkRepository()
    storage = FakeStorageService()
    index_service = FakeIndexService(submit_exc=submit_exc)
    service = KnowledgeBaseService(
        knowledge_base_repository=kb_repo,
        permission_repository=permission_repo,
        document_repository=document_repo,
        task_repository=task_repo,
        chunk_repository=chunk_repo,
        storage_service=storage,
        index_service=index_service,
        max_upload_file_size_mb=50,
    )
    return ServiceBundle(
        service,
        kb_repo,
        permission_repo,
        document_repo,
        task_repo,
        chunk_repo,
        storage,
        index_service,
    )


@pytest.mark.asyncio
async def test_create_knowledge_base_grants_creator_admin_permission() -> None:
    bundle = _bundle()

    kb = await bundle.service.create(
        KnowledgeBaseCreateRequest(
            name="技术文档库",
            description="团队规范",
            department_id="engineering",
            is_public=False,
        ),
        _user(),
    )

    assert kb.id == 10
    assert bundle.kb_repo.created[0]["created_by"] == 1
    assert bundle.permission_repo.created[0].kb_id == 10
    assert bundle.permission_repo.created[0].permission == "ADMIN"


@pytest.mark.asyncio
async def test_upload_document_stores_file_creates_document_and_submits_index_task() -> None:
    bundle = _bundle()

    document = await bundle.service.upload_document(10, FakeUploadFile("handbook.txt"), _user())

    assert document.id == 1
    assert document.status == DocumentStatus.PENDING.value
    assert bundle.storage.uploaded == [(10, "handbook.txt")]
    assert bundle.document_repo.created[0]["minio_path"] == "kb/10/abc-handbook.txt"
    assert bundle.document_repo.created[0]["file_type"] == "TXT"
    assert bundle.index_service.submitted == [1]


@pytest.mark.asyncio
async def test_upload_document_rejects_unsupported_file_type() -> None:
    bundle = _bundle()

    with pytest.raises(HTTPException) as exc_info:
        await bundle.service.upload_document(10, FakeUploadFile("virus.exe"), _user())

    assert exc_info.value.status_code == 400
    assert bundle.storage.uploaded == []
    assert bundle.document_repo.created == []


@pytest.mark.asyncio
async def test_upload_document_marks_document_failed_when_index_submit_fails() -> None:
    bundle = _bundle(submit_exc=RuntimeError("index submit failed"))

    with pytest.raises(RuntimeError, match="index submit failed"):
        await bundle.service.upload_document(10, FakeUploadFile("handbook.txt"), _user())

    document = bundle.document_repo.documents[1]
    assert document.status == DocumentStatus.FAILED.value
    assert document.error_msg == "index submit failed"
    assert bundle.storage.deleted == ["kb/10/abc-handbook.txt"]


@pytest.mark.asyncio
async def test_get_index_status_returns_document_and_latest_task_state() -> None:
    bundle = _bundle()
    document = await bundle.service.upload_document(10, FakeUploadFile("handbook.txt"), _user())
    document.status = DocumentStatus.FAILED.value
    document.error_msg = "embedding timeout"
    document.chunk_count = 3
    document.token_count = 120
    document.indexed_at = datetime(2026, 7, 4, tzinfo=UTC).replace(tzinfo=None)
    bundle.task_repo.latest[document.id] = IndexTask(
        id=9,
        doc_id=document.id,
        status=IndexTaskStatus.FAILED.value,
        retry_count=2,
    )

    status = await bundle.service.get_index_status(10, document.id)

    assert status.doc_id == document.id
    assert status.status == DocumentStatus.FAILED.value
    assert status.error_msg == "embedding timeout"
    assert status.chunk_count == 3
    assert status.token_count == 120
    assert status.retry_count == 2


@pytest.mark.asyncio
async def test_delete_document_soft_deletes_document_hard_deletes_chunks_and_deletes_minio() -> None:
    bundle = _bundle()
    document = await bundle.service.upload_document(10, FakeUploadFile("handbook.txt"), _user())

    await bundle.service.delete_document(10, document.id)

    assert document.is_deleted is True
    assert bundle.chunk_repo.deleted_doc_ids == [document.id]
    assert bundle.storage.deleted == ["kb/10/abc-handbook.txt"]


@pytest.mark.asyncio
async def test_reindex_document_resets_document_and_submits_reindex_task() -> None:
    bundle = _bundle()
    document = await bundle.service.upload_document(10, FakeUploadFile("handbook.txt"), _user())
    document.status = DocumentStatus.FAILED.value
    document.error_msg = "parse failed"

    task_id = await bundle.service.reindex_document(10, document.id)

    assert task_id == 201
    assert document.status == DocumentStatus.PENDING.value
    assert document.error_msg is None
    assert bundle.index_service.reindexed == [document.id]
