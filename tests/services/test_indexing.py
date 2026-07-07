from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
        self.processing_doc_ids: list[int] = []

    async def get(self, doc_id: int) -> KbDocument | None:
        return self.docs.get(doc_id)

    async def mark_processing(self, doc_id: int) -> None:
        self.processing_doc_ids.append(doc_id)
        self.docs[doc_id].status = DocumentStatus.PROCESSING.value

    async def mark_done(
        self,
        doc_id: int,
        *,
        chunk_count: int,
        token_count: int,
        version: int,
        file_name: str | None = None,
        file_type: str | None = None,
        file_size: int | None = None,
        minio_path: str | None = None,
    ) -> None:
        doc = self.docs[doc_id]
        doc.status = DocumentStatus.DONE.value
        doc.error_msg = None
        doc.chunk_count = chunk_count
        doc.token_count = token_count
        doc.version = version
        if file_name is not None:
            doc.file_name = file_name
        if file_type is not None:
            doc.file_type = file_type
        if file_size is not None:
            doc.file_size = file_size
        if minio_path is not None:
            doc.minio_path = minio_path

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
        payload: dict[str, object] | None = None,
    ) -> IndexTask:
        task = IndexTask(
            id=self.next_id,
            doc_id=doc_id,
            task_type=task_type.value,
            status=IndexTaskStatus.PENDING.value,
            retry_count=0,
            max_retry=3,
            payload=payload,
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
        self.deleted: list[str] = []

    async def download(self, object_key: str) -> bytes:
        self.download_calls.append(object_key)
        if self.exc:
            raise self.exc
        assert self.data is not None
        return self.data

    async def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)


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


class ThreadRecordingLoader(FakeLoader):
    def __init__(self, docs: list[Document]) -> None:
        super().__init__(docs)
        self.thread_id: int | None = None

    def load(self, file: BytesIO, file_name: str) -> list[Document]:
        self.thread_id = threading.get_ident()
        return super().load(file, file_name)


class ThreadRecordingChunkService(FakeChunkService):
    def __init__(self, chunks: list[Document]) -> None:
        super().__init__(chunks)
        self.thread_id: int | None = None

    def split_documents(self, docs: list[Document]) -> list[Document]:
        self.thread_id = threading.get_ident()
        return super().split_documents(docs)


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
    index_service_kwargs: dict[str, object] | None = None,
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
        **(index_service_kwargs or {}),
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
async def test_submit_index_task_commits_current_transaction_before_launch() -> None:
    events: list[str] = []

    async def commit_before_launch() -> None:
        events.append("commit")

    bundle = _build_service(
        index_service_kwargs={"commit_before_launch": commit_before_launch},
    )

    task_id = await bundle.service.submit_index_task(1)

    assert events == ["commit"]
    assert bundle.service.launched == [(task_id, 1)]


@pytest.mark.asyncio
async def test_launch_task_uses_background_service_factory() -> None:
    events: list[str] = []
    document_repo = FakeDocumentRepository([_build_document()])
    task_repo = FakeIndexTaskRepository()

    class RequestScopedIndexService(IndexService):
        async def run_task(self, task_id: int, doc_id: int) -> None:
            events.append(f"request:{task_id}:{doc_id}")

    class BackgroundIndexService:
        async def run_task(self, task_id: int, doc_id: int) -> None:
            events.append(f"background:{task_id}:{doc_id}")

    @asynccontextmanager
    async def background_service_factory() -> AsyncIterator[BackgroundIndexService]:
        events.append("factory-enter")
        yield BackgroundIndexService()
        events.append("factory-exit")

    service = RequestScopedIndexService(
        document_repository=document_repo,
        task_repository=task_repo,
        chunk_repository=FakeChunkRepository(),
        storage_service=FakeStorage(),
        loader_service=FakeLoader([Document(page_content="正文")]),
        chunk_service=FakeChunkService([Document(page_content="正文")]),
        embedding_service=FakeEmbeddingService([[0.1] * 1024]),
        background_service_factory=background_service_factory,
    )

    task_id = await service.submit_index_task(1)
    await asyncio.gather(*list(service._background_tasks))

    assert events == [
        "factory-enter",
        f"background:{task_id}:1",
        "factory-exit",
    ]


@pytest.mark.asyncio
async def test_submit_index_task_rejects_missing_document_without_creating_task() -> None:
    bundle = _build_service(docs=[])

    with pytest.raises(NonRetryableIndexError, match="document not found"):
        await bundle.service.submit_index_task(404)

    assert bundle.tasks.tasks == {}
    assert bundle.service.launched == []


@pytest.mark.asyncio
async def test_run_task_initial_index_keeps_document_initial_version() -> None:
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
    assert doc.version == 1
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
    assert inserted.doc_version == 1
    assert inserted.content_tsv is None
    assert bundle.chunks.deleted == [(1, 1)]


@pytest.mark.asyncio
async def test_run_task_logs_indexing_stage_start_and_finish(caplog: pytest.LogCaptureFixture) -> None:
    bundle = _build_service()
    task_id = await bundle.service.submit_index_task(1)

    with caplog.at_level(logging.INFO, logger="app.services.indexing"):
        await bundle.service.run_task(task_id, 1)

    messages = [record.getMessage() for record in caplog.records]
    assert "文档加载阶段开始了..." in messages
    assert "文档加载阶段结束了..." in messages
    assert "文档分块阶段开始了..." in messages
    assert "文档分块阶段结束了..." in messages
    assert "Embedding阶段开始了..." in messages
    assert "Embedding阶段结束了..." in messages
    assert any("chunk 入库完成" in message for message in messages)
    assert any("旧版本 chunk 清理完成" in message for message in messages)
    assert any("索引任务 DONE" in message for message in messages)


@pytest.mark.asyncio
async def test_run_task_offloads_parse_and_chunk_steps_from_event_loop_thread() -> None:
    main_thread_id = threading.get_ident()
    loader = ThreadRecordingLoader([Document(page_content="员工手册正文", metadata={"page_num": 1})])
    chunk_service = ThreadRecordingChunkService(
        [Document(page_content="员工手册正文", metadata={"page_num": 1, "estimated_tokens": 8})]
    )
    bundle = _build_service()
    bundle.service.loader_service = loader
    bundle.service.chunk_service = chunk_service
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    assert loader.thread_id is not None
    assert chunk_service.thread_id is not None
    assert loader.thread_id != main_thread_id
    assert chunk_service.thread_id != main_thread_id


@pytest.mark.asyncio
async def test_run_task_commits_running_and_processing_status_before_loading() -> None:
    events: list[str] = []
    doc = _build_document(version=1)

    async def commit_after_status_change() -> None:
        task = bundle.tasks.tasks[task_id]
        events.append(f"commit:{task.status}:{doc.status}")

    class RecordingStorage(FakeStorage):
        async def download(self, object_key: str) -> bytes:
            events.append("download")
            return await super().download(object_key)

    bundle = _build_service(
        docs=[doc],
        index_service_kwargs={"commit_after_status_change": commit_after_status_change},
    )
    bundle.service.storage_service = RecordingStorage()
    task_id = await bundle.service.submit_index_task(1)

    await bundle.service.run_task(task_id, 1)

    assert events[0] == "commit:RUNNING:PROCESSING"
    assert events[1] == "download"


@pytest.mark.asyncio
async def test_run_task_reindex_keeps_done_document_queryable_while_running() -> None:
    events: list[str] = []
    doc = _build_document(status=DocumentStatus.DONE.value, version=1)

    async def commit_after_status_change() -> None:
        task = bundle.tasks.tasks[task_id]
        events.append(f"commit:{task.status}:{doc.status}")

    bundle = _build_service(
        docs=[doc],
        index_service_kwargs={"commit_after_status_change": commit_after_status_change},
    )
    task_id = await bundle.service.reindex_document(1)

    await bundle.service.run_task(task_id, 1)

    assert events[0] == "commit:RUNNING:DONE"
    assert bundle.documents.processing_doc_ids == []
    assert doc.status == DocumentStatus.DONE.value


@pytest.mark.asyncio
async def test_run_task_reindex_increments_document_version() -> None:
    doc = _build_document(version=1)
    bundle = _build_service(
        docs=[doc],
        chunks=[
            Document(
                page_content="重建后的第一块内容",
                metadata={"chunk_index": 0, "page_num": 2, "estimated_tokens": 10},
            )
        ],
        vectors=[[0.4] * 1024],
    )
    task_id = await bundle.service.reindex_document(1)

    await bundle.service.run_task(task_id, 1)

    task = bundle.tasks.tasks[task_id]
    assert task.task_type == IndexTaskType.REINDEX.value
    assert task.status == IndexTaskStatus.DONE.value
    assert doc.status == DocumentStatus.DONE.value
    assert doc.version == 2
    inserted = bundle.chunks.inserted[0]
    assert inserted.doc_version == 2
    assert bundle.chunks.deleted == [(1, 2)]


@pytest.mark.asyncio
async def test_run_task_reindex_with_replacement_payload_publishes_new_file_after_success() -> None:
    doc = _build_document(
        status=DocumentStatus.DONE.value,
        version=2,
        file_name="old-handbook.txt",
        file_type="TXT",
        file_size=64,
        minio_path="kb/10/old-handbook.txt",
    )
    bundle = _build_service(
        docs=[doc],
        chunks=[Document(page_content="新版制度", metadata={"estimated_tokens": 4})],
        vectors=[[0.5] * 1024],
    )
    task_id = await bundle.service.reindex_document(
        1,
        payload={
            "source": {
                "file_name": "updated.pdf",
                "file_type": "PDF",
                "file_size": 256,
                "minio_path": "kb/10/new-updated.pdf",
            },
            "old_minio_path": "kb/10/old-handbook.txt",
        },
    )

    await bundle.service.run_task(task_id, 1)

    assert bundle.storage.download_calls == ["kb/10/new-updated.pdf"]
    assert doc.status == DocumentStatus.DONE.value
    assert doc.version == 3
    assert doc.file_name == "updated.pdf"
    assert doc.file_type == "PDF"
    assert doc.file_size == 256
    assert doc.minio_path == "kb/10/new-updated.pdf"
    assert bundle.chunks.inserted[0].doc_version == 3
    assert bundle.chunks.deleted == [(1, 3)]
    assert bundle.storage.deleted == ["kb/10/old-handbook.txt"]


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
