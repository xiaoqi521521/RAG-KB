from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.models import DocumentStatus, KbDocument
from app.services.document_update import DocumentUpdateService


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=1,
        department_id="engineering",
        role="ADMIN",
    )


@dataclass
class FakeUploadFile:
    filename: str | None
    size: int | None = 128
    content_type: str = "text/plain"


class FakeDocumentRepository:
    def __init__(self, document: KbDocument | None) -> None:
        self.document = document
        self.replaced: list[dict[str, object]] = []
        self.reset_doc_ids: list[int] = []
        self.failed: list[tuple[int, str]] = []

    async def get_active_in_kb(self, kb_id: int, doc_id: int) -> KbDocument | None:
        if self.document is None:
            return None
        if self.document.id != doc_id or self.document.kb_id != kb_id or self.document.is_deleted:
            return None
        return self.document

    async def reset_for_reindex(self, doc_id: int) -> None:
        assert self.document is not None
        self.reset_doc_ids.append(doc_id)
        self.document.status = DocumentStatus.PENDING.value
        self.document.error_msg = None
        self.document.chunk_count = None
        self.document.token_count = None
        self.document.indexed_at = None

    async def mark_failed(self, doc_id: int, error_msg: str) -> None:
        assert self.document is not None
        self.failed.append((doc_id, error_msg))
        self.document.status = DocumentStatus.FAILED.value
        self.document.error_msg = error_msg


class FakeStorageService:
    def __init__(self) -> None:
        self.uploaded: list[tuple[int, str | None]] = []
        self.deleted: list[str] = []

    async def upload(self, kb_id: int, file: FakeUploadFile) -> str:
        self.uploaded.append((kb_id, file.filename))
        return f"kb/{kb_id}/new-{file.filename}"

    async def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)


class FakeIndexService:
    def __init__(self, exc: Exception | None = None) -> None:
        self.reindexed: list[tuple[int, dict[str, object] | None]] = []
        self.exc = exc

    async def reindex_document(
        self,
        doc_id: int,
        *,
        payload: dict[str, object] | None = None,
    ) -> int:
        self.reindexed.append((doc_id, payload))
        if self.exc is not None:
            raise self.exc
        return 200 + doc_id


@dataclass
class ServiceBundle:
    service: DocumentUpdateService
    document: KbDocument
    document_repo: FakeDocumentRepository
    storage: FakeStorageService
    index_service: FakeIndexService


def _document(*, status: str = DocumentStatus.DONE.value) -> KbDocument:
    return KbDocument(
        id=7,
        kb_id=10,
        file_name="handbook.txt",
        file_type="TXT",
        file_size=64,
        minio_path="kb/10/old-handbook.txt",
        uploaded_by=1,
        status=status,
        version=2,
        chunk_count=3,
        token_count=120,
    )


def _bundle(
    *,
    document: KbDocument | None = None,
    index_exc: Exception | None = None,
) -> ServiceBundle:
    doc = document or _document()
    document_repo = FakeDocumentRepository(doc)
    storage = FakeStorageService()
    index_service = FakeIndexService(index_exc)
    service = DocumentUpdateService(
        document_repository=document_repo,
        storage_service=storage,
        index_service=index_service,
        max_upload_file_size_mb=50,
    )
    return ServiceBundle(service, doc, document_repo, storage, index_service)


@pytest.mark.asyncio
async def test_replace_content_updates_existing_document_submits_reindex_and_deletes_old_file() -> None:
    bundle = _bundle()

    response = await bundle.service.replace_content(
        10,
        7,
        FakeUploadFile("updated.pdf", size=256, content_type="application/pdf"),
        _user(),
    )

    assert response.doc_id == 7
    assert response.file_name == "handbook.txt"
    assert response.status == DocumentStatus.DONE.value
    assert response.task_id == 207
    assert bundle.document_repo.replaced == []
    assert bundle.index_service.reindexed == [
        (
            7,
            {
                "source": {
                    "file_name": "updated.pdf",
                    "file_type": "PDF",
                    "file_size": 256,
                    "minio_path": "kb/10/new-updated.pdf",
                },
                "old_minio_path": "kb/10/old-handbook.txt",
            },
        )
    ]
    assert bundle.storage.deleted == []
    assert bundle.document.file_name == "handbook.txt"
    assert bundle.document.minio_path == "kb/10/old-handbook.txt"
    assert bundle.document.status == DocumentStatus.DONE.value
    assert bundle.document.chunk_count == 3
    assert bundle.document.token_count == 120


@pytest.mark.asyncio
async def test_force_reindex_resets_existing_document_without_uploading_file() -> None:
    bundle = _bundle()

    response = await bundle.service.force_reindex(10, 7)

    assert response.doc_id == 7
    assert response.file_name == "handbook.txt"
    assert response.status == DocumentStatus.DONE.value
    assert response.task_id == 207
    assert bundle.document_repo.reset_doc_ids == []
    assert bundle.storage.uploaded == []
    assert bundle.index_service.reindexed == [(7, None)]
    assert bundle.document.chunk_count == 3
    assert bundle.document.token_count == 120


@pytest.mark.asyncio
async def test_replace_content_rejects_processing_document_before_upload() -> None:
    bundle = _bundle(document=_document(status=DocumentStatus.PROCESSING.value))

    with pytest.raises(HTTPException) as exc_info:
        await bundle.service.replace_content(10, 7, FakeUploadFile("updated.txt"), _user())

    assert exc_info.value.status_code == 409
    assert bundle.storage.uploaded == []
    assert bundle.index_service.reindexed == []


@pytest.mark.asyncio
async def test_replace_content_marks_failed_and_keeps_new_file_when_reindex_submit_fails() -> None:
    bundle = _bundle(index_exc=RuntimeError("index submit failed"))

    with pytest.raises(RuntimeError, match="index submit failed"):
        await bundle.service.replace_content(10, 7, FakeUploadFile("updated.txt"), _user())

    assert bundle.storage.deleted == ["kb/10/new-updated.txt"]
    assert bundle.document_repo.failed == []
    assert bundle.document.status == DocumentStatus.DONE.value
