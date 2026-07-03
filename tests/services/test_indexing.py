from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import pytest
from langchain_core.documents import Document

from app.models import (
    DocChunk,
    DocumentStatus,
    IndexTask,
    IndexTaskStatus,
    IndexTaskType,
    KbDocument,
)
from app.services.indexing import IndexService, NonRetryableIndexError, RetryableIndexError


def _build_document(**overrides: object) -> KbDocument:
    values = {
        "id": 1,
        "kb_id": 10,
        "file_name": "handbook.txt",
        "file_type": "TXT",
        "file_size": 128,
        "minio_path": "kb/10/handbook.txt",
        "status": DocumentStatus.PENDING.value,
        "version": 1,
        "uploaded_by": 99,
    }
    values.update(overrides)
    return KbDocument(**values)


class FakeDocumentRepository:
    def __init__(self, docs: list[KbDocument]) -> None:
        self.docs = {doc.id: doc for doc in docs}

    async def get(self, doc_id: int) -> KbDocument | None:
        return self.docs.get(doc_id)

    async def mark_processing(self, doc_id: int) -> None:
        self.docs[doc_id].status = DocumentStatus.PROCESSING.value

    async def mark_done(
        self,
        doc_id: int,
        *,
        chunk_count: int,
        token_count: int,
        version: int,
    ) -> None:
        doc = self.docs[doc_id]
        doc.status = DocumentStatus.DONE.value
        doc.error_msg = None
        doc.chunk_count = chunk_count
        doc.token_count = token_count
        doc.version = version

    async def mark_failed(self, doc_id: int, error_msg: str) -> None:
        doc = self.docs[doc_id]
        doc.status = DocumentStatus.FAILED.value
        doc.error_msg = error_msg


class FakeIndexTaskRepository:
    def __init__(self) -> None:
        self.tasks: dict[int, IndexTask] = {}
        self.next_id = 1

    async def create(
        self,
        doc_id: int,
        *,
        task_type: IndexTaskType = IndexTaskType.INDEX,
    ) -> IndexTask:
        task = IndexTask(
            id=self.next_id,
            doc_id=doc_id,
            task_type=task_type.value,
            status=IndexTaskStatus.PENDING.value,
            retry_count=0,
            max_retry=3,
        )
        self.tasks[task.id] = task
        self.next_id += 1
        return task

    async def get(self, task_id: int) -> IndexTask | None:
        return self.tasks.get(task_id)

    async def mark_running(self, task_id: int) -> None:
        self.tasks[task_id].status = IndexTaskStatus.RUNNING.value

    async def mark_done(self, task_id: int) -> None:
        task = self.tasks[task_id]
        task.status = IndexTaskStatus.DONE.value
        task.error_msg = None

    async def mark_failed(self, task_id: int, error_msg: str) -> None:
        task = self.tasks[task_id]
        task.status = IndexTaskStatus.FAILED.value
        task.error_msg = error_msg

    async def mark_retry_pending(
        self,
        task_id: int,
        *,
        retry_count: int,
        error_msg: str,
    ) -> None:
        task = self.tasks[task_id]
        task.status = IndexTaskStatus.PENDING.value
        task.retry_count = retry_count
        task.error_msg = error_msg


class FakeChunkRepository:
    def __init__(self, insert_exc: Exception | None = None) -> None:
        self.inserted: list[DocChunk] = []
        self.deleted: list[tuple[int, int]] = []
        self.insert_exc = insert_exc

    async def insert_many(self, chunks: list[DocChunk]) -> None:
        if self.insert_exc:
            raise self.insert_exc
        self.inserted.extend(chunks)

    async def delete_older_versions(self, doc_id: int, current_version: int) -> None:
        self.deleted.append((doc_id, current_version))


class FakeStorage:
    def __init__(self, data: bytes | None = b"hello", exc: Exception | None = None) -> None:
        self.data = data
        self.exc = exc
        self.download_calls: list[str] = []

    async def download(self, object_key: str) -> bytes:
        self.download_calls.append(object_key)
        if self.exc:
            raise self.exc
        assert self.data is not None
        return self.data


class FakeLoader:
    def __init__(self, docs: list[Document], exc: Exception | None = None) -> None:
        self.docs = docs
        self.exc = exc
        self.calls: list[tuple[BytesIO, str]] = []

    def load(self, file: BytesIO, file_name: str) -> list[Document]:
        self.calls.append((file, file_name))
        if self.exc:
            raise self.exc
        return self.docs


class FakeChunkService:
    def __init__(self, chunks: list[Document]) -> None:
        self.chunks = chunks

    def split_documents(self, docs: list[Document]) -> list[Document]:
        return self.chunks


class FakeEmbeddingService:
    def __init__(self, vectors: list[list[float]], exc: Exception | None = None) -> None:
        self.vectors = vectors
        self.exc = exc
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.exc:
            raise self.exc
        return self.vectors


@dataclass
class ServiceBundle:
    service: "RecordingIndexService"
    documents: FakeDocumentRepository
    tasks: FakeIndexTaskRepository
    chunks: FakeChunkRepository
    storage: FakeStorage
    embedding: FakeEmbeddingService


class RecordingIndexService(IndexService):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.launched: list[tuple[int, int]] = []
        self.scheduled: list[tuple[int, int, int]] = []

    def _launch_task(self, task_id: int, doc_id: int) -> None:
        self.launched.append((task_id, doc_id))

    def _schedule_retry(self, task_id: int, doc_id: int, retry_count: int) -> None:
        self.scheduled.append((task_id, doc_id, retry_count))


def _build_service(
    *,
    docs: list[KbDocument] | None = None,
    parsed_docs: list[Document] | None = None,
    chunks: list[Document] | None = None,
    vectors: list[list[float]] | None = None,
    storage_exc: Exception | None = None,
    loader_exc: Exception | None = None,
    embedding_exc: Exception | None = None,
    chunk_insert_exc: Exception | None = None,
) -> ServiceBundle:
    document_repo = FakeDocumentRepository(docs or [_build_document()])
    task_repo = FakeIndexTaskRepository()
    chunk_repo = FakeChunkRepository(insert_exc=chunk_insert_exc)
    storage = FakeStorage(exc=storage_exc)
    loader = FakeLoader(
        parsed_docs or [Document(page_content="员工手册正文", metadata={"page_num": 1})],
        exc=loader_exc,
    )
    chunk_service = FakeChunkService(
        chunks
        if chunks is not None
        else [
            Document(
                page_content="员工手册正文",
                metadata={"chunk_index": 0, "page_num": 1, "estimated_tokens": 8},
            )
        ]
    )
    embedding = FakeEmbeddingService(vectors or [[0.1] * 1024], exc=embedding_exc)
    service = RecordingIndexService(
        document_repository=document_repo,
        task_repository=task_repo,
        chunk_repository=chunk_repo,
        storage_service=storage,
        loader_service=loader,
        chunk_service=chunk_service,
        embedding_service=embedding,
    )
    return ServiceBundle(service, document_repo, task_repo, chunk_repo, storage, embedding)


@pytest.mark.asyncio
async def test_submit_index_task_creates_pending_task_and_launches_background_run() -> None:
    bundle = _build_service()

    task_id = await bundle.service.submit_index_task(1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.PENDING.value
    assert task.task_type == IndexTaskType.INDEX.value
    assert bundle.service.launched == [(task_id, 1)]


@pytest.mark.asyncio
async def test_submit_index_task_rejects_missing_document_without_creating_task() -> None:
    bundle = _build_service(docs=[])

    with pytest.raises(NonRetryableIndexError, match="document not found"):
        await bundle.service.submit_index_task(404)

    assert bundle.tasks.tasks == {}
    assert bundle.service.launched == []


@pytest.mark.asyncio
async def test_run_task_success_indexes_chunks_and_marks_document_done() -> None:
    doc = _build_document(version=1)
    bundle = _build_service(
        docs=[doc],
        chunks=[
            Document(
                page_content="第一块内容",
                metadata={
                    "chunk_index": 0,
                    "page_num": 2,
                    "section_title": "总则",
                    "estimated_tokens": 12,
                },
            )
        ],
        vectors=[[0.2] * 1024],
    )
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.DONE.value
    assert doc.status == DocumentStatus.DONE.value
    assert doc.version == 2
    assert doc.chunk_count == 1
    assert doc.token_count == 12
    assert bundle.storage.download_calls == ["kb/10/handbook.txt"]
    assert bundle.embedding.calls == [["第一块内容"]]
    assert len(bundle.chunks.inserted) == 1
    inserted = bundle.chunks.inserted[0]
    assert inserted.doc_id == 1
    assert inserted.kb_id == 10
    assert inserted.chunk_index == 0
    assert inserted.content == "第一块内容"
    assert inserted.embedding == [0.2] * 1024
    assert inserted.page_num == 2
    assert inserted.section_title == "总则"
    assert inserted.token_count == 12
    assert inserted.doc_version == 2
    assert bundle.chunks.deleted == [(1, 2)]


@pytest.mark.asyncio
async def test_run_task_retries_without_inserting_when_split_result_is_empty() -> None:
    doc = _build_document()
    bundle = _build_service(docs=[doc], chunks=[])
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.PENDING.value
    assert task.retry_count == 1
    assert "no valid chunks" in task.error_msg
    assert doc.status == DocumentStatus.FAILED.value
    assert bundle.chunks.inserted == []
    assert bundle.service.scheduled == [(task_id, 1, 1)]


@pytest.mark.asyncio
async def test_run_task_retries_without_inserting_when_embedding_count_mismatches() -> None:
    doc = _build_document()
    bundle = _build_service(
        docs=[doc],
        chunks=[
            Document(page_content="第一块", metadata={"chunk_index": 0, "page_num": 1}),
            Document(page_content="第二块", metadata={"chunk_index": 1, "page_num": 1}),
        ],
        vectors=[[0.3] * 1024],
    )
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.PENDING.value
    assert task.retry_count == 1
    assert "embedding result count mismatch" in task.error_msg
    assert doc.status == DocumentStatus.FAILED.value
    assert bundle.chunks.inserted == []
    assert bundle.service.scheduled == [(task_id, 1, 1)]


@pytest.mark.asyncio
async def test_run_task_retries_when_loader_fails() -> None:
    doc = _build_document()
    bundle = _build_service(docs=[doc], loader_exc=RuntimeError("parse service timeout"))
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.PENDING.value
    assert task.retry_count == 1
    assert "parse service timeout" in task.error_msg
    assert doc.status == DocumentStatus.FAILED.value
    assert bundle.chunks.inserted == []
    assert bundle.service.scheduled == [(task_id, 1, 1)]


@pytest.mark.asyncio
async def test_run_task_retries_when_embedding_fails() -> None:
    doc = _build_document()
    bundle = _build_service(docs=[doc], embedding_exc=RuntimeError("embedding timeout"))
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.PENDING.value
    assert task.retry_count == 1
    assert "embedding timeout" in task.error_msg
    assert doc.status == DocumentStatus.FAILED.value
    assert bundle.chunks.inserted == []
    assert bundle.service.scheduled == [(task_id, 1, 1)]


@pytest.mark.asyncio
async def test_run_task_retries_when_chunk_insert_fails() -> None:
    doc = _build_document()
    bundle = _build_service(docs=[doc], chunk_insert_exc=RuntimeError("db connection lost"))
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.PENDING.value
    assert task.retry_count == 1
    assert "db connection lost" in task.error_msg
    assert doc.status == DocumentStatus.FAILED.value
    assert bundle.chunks.inserted == []
    assert bundle.service.scheduled == [(task_id, 1, 1)]


@pytest.mark.asyncio
async def test_run_task_retryable_failure_marks_task_pending_and_schedules_retry() -> None:
    doc = _build_document()
    bundle = _build_service(
        docs=[doc],
        storage_exc=RetryableIndexError("temporary minio error"),
    )
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.PENDING.value
    assert task.retry_count == 1
    assert task.error_msg == "temporary minio error"
    assert doc.status == DocumentStatus.FAILED.value
    assert bundle.service.scheduled == [(task_id, 1, 1)]


@pytest.mark.asyncio
async def test_run_task_retryable_failure_stays_failed_after_max_retry() -> None:
    doc = _build_document()
    bundle = _build_service(
        docs=[doc],
        storage_exc=RetryableIndexError("temporary minio error"),
    )
    task_id = await bundle.service.submit_index_task(1)
    bundle.tasks.tasks[task_id].retry_count = 3

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.status == IndexTaskStatus.FAILED.value
    assert task.retry_count == 3
    assert bundle.service.scheduled == []
