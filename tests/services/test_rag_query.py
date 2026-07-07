from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.repositories.chunks import ChunkSearchHit
from app.schemas.rag import SourceCitation
from app.services.rag_query import RAG_REFUSAL_ANSWER, RagQueryService


def _user() -> CurrentUser:
    return CurrentUser(user_id=1, department_id="engineering", role="ADMIN")


def _hit(*, chunk_id: int = 10, score: float = 0.9) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=1,
        document_name="研发规范.md",
        kb_id=2,
        chunk_index=3,
        content="代码提交前必须通过本地测试。",
        page_num=None,
        section_title="代码提交",
        score=score,
    )


class FakeEmbeddingService:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.1, 0.2]


class FakeChunkRepository:
    def __init__(self, hits: list[ChunkSearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict[str, object]] = []

    async def search_by_vector(
        self,
        *,
        query_vector: list[float],
        kb_ids: list[int],
        top_k: int,
    ) -> list[ChunkSearchHit]:
        self.calls.append({"query_vector": query_vector, "kb_ids": kb_ids, "top_k": top_k})
        return self.hits


class FakeSourceBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def build(
        self,
        hits: list[ChunkSearchHit],
        *,
        return_top_n: int,
    ) -> tuple[str, list[SourceCitation]]:
        self.calls.append({"hits": hits, "return_top_n": return_top_n})
        return (
            "[参考1]\n内容：代码提交前必须通过本地测试。",
            [
                SourceCitation(
                    document_id=1,
                    document_name="研发规范.md",
                    kb_id=2,
                    chunk_id=hit.chunk_id,
                    chunk_index=3,
                    page_number=None,
                    section_title="代码提交",
                    score=hit.score,
                )
                for hit in hits
            ],
        )


class FakeChatModel:
    def __init__(self, content: object = "需要通过本地测试。[参考1]") -> None:
        self.content = content
        self.messages: list[object] | None = None

    async def ainvoke(self, messages: list[object]) -> SimpleNamespace:
        self.messages = messages
        return SimpleNamespace(content=self.content)


@dataclass
class FakeSettings:
    rag_vector_top_k: int = 20
    rag_return_top_n: int = 5
    rag_min_score: float = 0.5


def _service(
    *,
    hits: list[ChunkSearchHit],
    chat_content: object = "需要通过本地测试。[参考1]",
    min_score: float = 0.5,
) -> tuple[RagQueryService, FakeEmbeddingService, FakeChunkRepository, FakeSourceBuilder, FakeChatModel]:
    embedding = FakeEmbeddingService()
    chunks = FakeChunkRepository(hits)
    source_builder = FakeSourceBuilder()
    chat = FakeChatModel(chat_content)
    service = RagQueryService(
        embedding_service=embedding,
        chunk_repository=chunks,
        source_builder=source_builder,
        chat_model=chat,
        settings=FakeSettings(rag_min_score=min_score),
    )
    return service, embedding, chunks, source_builder, chat


@pytest.mark.asyncio
async def test_query_returns_refusal_without_calling_chat_when_no_hits() -> None:
    service, embedding, chunks, source_builder, chat = _service(hits=[])

    response = await service.query(question=" 量子计算是什么？ ", kb_ids=[2], user=_user())

    assert response.answer == RAG_REFUSAL_ANSWER
    assert response.sources == []
    assert response.hit_count == 0
    assert embedding.queries == ["量子计算是什么？"]
    assert chunks.calls == [{"query_vector": [0.1, 0.2], "kb_ids": [2], "top_k": 20}]
    assert source_builder.calls == []
    assert chat.messages is None


@pytest.mark.asyncio
async def test_query_returns_refusal_without_calling_chat_when_top_score_is_low() -> None:
    service, _, _, source_builder, chat = _service(hits=[_hit(score=0.3)])

    response = await service.query(question="代码提交规范？", kb_ids=[2], user=_user())

    assert response.answer == RAG_REFUSAL_ANSWER
    assert response.sources == []
    assert response.hit_count == 0
    assert source_builder.calls == []
    assert chat.messages is None


@pytest.mark.asyncio
async def test_query_generates_answer_with_context_and_sources() -> None:
    service, _, _, source_builder, chat = _service(hits=[_hit(score=0.9)])

    response = await service.query(question="代码提交规范？", kb_ids=[2], user=_user())

    assert response.answer == "需要通过本地测试。[参考1]"
    assert response.sources[0].chunk_id == 10
    assert response.hit_count == 1
    assert response.latency_ms >= 0
    assert source_builder.calls[0]["return_top_n"] == 5
    assert chat.messages is not None
    assert "只能根据【参考内容】回答" in chat.messages[0].content
    assert "[参考1]" in chat.messages[0].content
    assert chat.messages[1].content == "代码提交规范？"


@pytest.mark.asyncio
async def test_query_filters_sources_below_min_score_before_generation() -> None:
    service, _, _, source_builder, _ = _service(
        hits=[_hit(chunk_id=10, score=0.76), _hit(chunk_id=11, score=0.68)],
        min_score=0.7,
    )

    response = await service.query(question="工作时间？", kb_ids=[2], user=_user())

    passed_hits = source_builder.calls[0]["hits"]
    assert [hit.chunk_id for hit in passed_hits] == [10]
    assert response.hit_count == 1
    assert [source.chunk_id for source in response.sources] == [10]


@pytest.mark.asyncio
async def test_query_raises_503_when_chat_returns_blank_answer() -> None:
    service, _, _, _, _ = _service(hits=[_hit(score=0.9)], chat_content="   ")

    with pytest.raises(HTTPException) as exc_info:
        await service.query(question="代码提交规范？", kb_ids=[2], user=_user())

    assert exc_info.value.status_code == 503
