import pytest
from langchain_core.documents import Document
from pydantic import ValidationError

from app.services.chunking import (
    ChunkConfig,
    ChunkService,
    ChunkSplitter,
)


def test_txt_document_uses_recursive_character_splitter_without_source_metadata():
    service = ChunkService()
    doc = Document(
        page_content="第一段。" * 80,
        metadata={"source": "handbook.txt", "file_type": "TXT", "page_num": 1},
    )

    chunks = service.split_documents(
        [doc],
        ChunkConfig(chunk_size=80, chunk_overlap=10, min_chunk_chars=20),
    )

    assert len(chunks) > 1
    assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.metadata["split_strategy"] == "recursive_character" for chunk in chunks)
    assert all(chunk.metadata["page_num"] == 1 for chunk in chunks)
    assert all("source" not in chunk.metadata for chunk in chunks)
    assert all("file_type" not in chunk.metadata for chunk in chunks)
    assert all("section_title" not in chunk.metadata for chunk in chunks)
    assert all(set(chunk.metadata) == {"chunk_index", "split_strategy", "estimated_tokens", "page_num"} for chunk in chunks)


def test_short_section_document_stays_as_one_structure_aware_chunk():
    service = ChunkService()
    doc = Document(
        page_content="请假制度\n员工请假需要提前提交申请。",
        metadata={"page_num": 3, "section_title": "请假制度"},
    )

    chunks = service.split_documents([doc], ChunkConfig(chunk_size=100, min_chunk_chars=1))

    assert len(chunks) == 1
    assert chunks[0].page_content == doc.page_content
    assert chunks[0].metadata == {
        "chunk_index": 0,
        "split_strategy": "structure_aware",
        "estimated_tokens": chunks[0].metadata["estimated_tokens"],
        "page_num": 3,
        "section_title": "请假制度",
    }
    assert chunks[0].metadata["estimated_tokens"] > 0


def test_long_section_document_splits_inside_section_and_keeps_section_title():
    service = ChunkService()
    doc = Document(
        page_content="考勤制度。" * 80,
        metadata={"page_num": 5, "section_title": "考勤制度"},
    )

    chunks = service.split_documents(
        [doc],
        ChunkConfig(chunk_size=80, chunk_overlap=10, min_chunk_chars=20),
    )

    assert len(chunks) > 1
    assert all(chunk.metadata["split_strategy"] == "structure_aware" for chunk in chunks)
    assert all(chunk.metadata["page_num"] == 5 for chunk in chunks)
    assert all(chunk.metadata["section_title"] == "考勤制度" for chunk in chunks)
    assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(range(len(chunks)))


def test_overlap_keeps_boundary_text_between_adjacent_chunks():
    service = ChunkService()
    doc = Document(page_content="abcdefghijklmnopqrstuvwxyz", metadata={"page_num": 1})

    chunks = service.split_documents(
        [doc],
        ChunkConfig(chunk_size=10, chunk_overlap=3, min_chunk_chars=1),
    )

    assert len(chunks) > 1
    assert chunks[0].page_content[-3:] == chunks[1].page_content[:3]


def test_short_chunks_are_filtered_and_global_chunk_index_is_reassigned():
    service = ChunkService()
    docs = [
        Document(page_content="短", metadata={"page_num": 1}),
        Document(page_content="这是一段足够长的正文。" * 10, metadata={"page_num": 2}),
    ]

    chunks = service.split_documents(
        docs,
        ChunkConfig(chunk_size=100, chunk_overlap=10, min_chunk_chars=20),
    )

    assert chunks
    assert all(chunk.metadata["page_num"] == 2 for chunk in chunks)
    assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(range(len(chunks)))


def test_invalid_chunk_config_raises_clear_error():
    with pytest.raises(ValidationError, match="chunk_overlap must be less than chunk_size"):
        ChunkConfig(chunk_size=100, chunk_overlap=100)


class FakeSplitter:
    strategy_name = "fake"

    def split(self, doc: Document, config: ChunkConfig) -> list[Document]:
        return [
            Document(
                page_content=doc.page_content,
                metadata={
                    "split_strategy": self.strategy_name,
                    "estimated_tokens": 1,
                    "page_num": doc.metadata["page_num"],
                },
            )
        ]


def test_chunk_service_can_use_chunk_splitter_interface_for_extension():
    fake_splitter: ChunkSplitter = FakeSplitter()
    service = ChunkService(
        recursive_splitter=fake_splitter,
        structure_aware_splitter=fake_splitter,
    )

    chunks = service.split_documents(
        [Document(page_content="自定义切分器正文", metadata={"page_num": 9})],
        ChunkConfig(min_chunk_chars=1),
    )

    assert len(chunks) == 1
    assert chunks[0].metadata == {
        "chunk_index": 0,
        "split_strategy": "fake",
        "estimated_tokens": 1,
        "page_num": 9,
    }


def test_chunk_config_reads_chunk_size_and_overlap_from_env(monkeypatch):
    monkeypatch.setenv("RAG_CHUNK_SIZE", "30")
    monkeypatch.setenv("RAG_CHUNK_OVERLAP", "5")

    config = ChunkConfig(_env_file=None)
    service = ChunkService()
    doc = Document(page_content="abcdefghijklmnopqrstuvwxyz" * 4, metadata={"page_num": 1})

    chunks = service.split_documents([doc], config)

    assert len(chunks) > 1
    assert chunks[0].page_content[-5:] == chunks[1].page_content[:5]
    assert all(len(chunk.page_content) <= 30 for chunk in chunks)
