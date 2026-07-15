from __future__ import annotations

from sqlalchemy import Float

from app.repositories.chunks import ChunkRepository


class FakeResult:
    def all(self) -> list[tuple[int, int, str, int, int, str, int | None, str | None, float]]:
        return [
            (10, 1, "研发规范.md", 2, 3, "代码提交前必须通过本地测试。", None, "代码提交", 0.25)
        ]


class FakeSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return FakeResult()


class FakeIdScalars:
    def all(self) -> list[int]:
        return [101, 102]


class FakeIdResult:
    def scalars(self) -> FakeIdScalars:
        return FakeIdScalars()


class IdRecordingSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return FakeIdResult()


async def test_search_by_vector_builds_current_version_filtered_query() -> None:
    session = FakeSession()
    repository = ChunkRepository(session)

    hits = await repository.search_by_vector(query_vector=[0.1, 0.2], kb_ids=[2, 3], top_k=5)

    statement_text = str(session.statement)
    assert "kb_doc_chunk.embedding <=> :embedding_1" in statement_text
    assert "kb_doc_chunk.kb_id IN" in statement_text
    assert "kb_doc_chunk.doc_version = kb_document.version" in statement_text
    assert "kb_document.status = :status_1" in statement_text
    assert "kb_document.is_deleted IS false" in statement_text
    assert isinstance(list(session.statement.selected_columns)[-1].type, Float)
    assert hits[0].chunk_id == 10
    assert hits[0].score == 0.8


async def test_search_by_fulltext_builds_current_version_filtered_query() -> None:
    session = FakeSession()
    repository = ChunkRepository(session)

    hits = await repository.search_by_fulltext(query_text="Commit & Message", kb_ids=[2, 3], top_k=5)

    statement_text = str(session.statement)
    assert "to_tsquery" in statement_text
    assert "websearch_to_tsquery" not in statement_text
    assert "ts_rank" in statement_text
    assert "content_tsv" in statement_text
    assert "to_tsvector" not in statement_text
    assert "kb_doc_chunk.kb_id IN" in statement_text
    assert "kb_doc_chunk.doc_version = kb_document.version" in statement_text
    assert "kb_document.status = :status_1" in statement_text
    assert "kb_document.is_deleted IS false" in statement_text
    assert isinstance(list(session.statement.selected_columns)[-1].type, Float)
    assert hits[0].chunk_id == 10
    assert hits[0].score == 0.25


async def test_list_older_version_ids_reads_only_document_chunks_before_new_version() -> None:
    """发布新版本前应只读取该文档即将删除的旧 chunk ID。"""
    session = IdRecordingSession()
    repository = ChunkRepository(session)

    chunk_ids = await repository.list_older_version_ids(doc_id=7, current_version=3)

    statement_text = str(session.statement)
    assert "kb_doc_chunk.doc_id =" in statement_text
    assert "kb_doc_chunk.doc_version <" in statement_text
    assert chunk_ids == [101, 102]
