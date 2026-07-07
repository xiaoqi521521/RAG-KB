from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.repositories.chunks import ChunkSearchHit
from app.services.hybrid_retriever import HybridRetriever, _rrf_fuse


def _hit(chunk_id: int, score: float = 0.9) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=1,
        document_name="policy.md",
        kb_id=2,
        chunk_index=chunk_id,
        content=f"chunk-{chunk_id}",
        page_num=None,
        section_title=None,
        score=score,
    )


class FakeEmbeddingService:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.1, 0.2]


class FakeChunkRepository:
    def __init__(self, vector_hits: list[ChunkSearchHit], fulltext_hits: list[ChunkSearchHit]) -> None:
        self.vector_hits = vector_hits
        self.fulltext_hits = fulltext_hits
        self.vector_calls: list[dict[str, object]] = []
        self.fulltext_calls: list[dict[str, object]] = []

    async def search_by_vector(
        self,
        *,
        query_vector: list[float],
        kb_ids: list[int],
        top_k: int,
    ) -> list[ChunkSearchHit]:
        self.vector_calls.append({"query_vector": query_vector, "kb_ids": kb_ids, "top_k": top_k})
        return self.vector_hits

    async def search_by_fulltext(
        self,
        *,
        query_text: str,
        kb_ids: list[int],
        top_k: int,
    ) -> list[ChunkSearchHit]:
        self.fulltext_calls.append({"query_text": query_text, "kb_ids": kb_ids, "top_k": top_k})
        return self.fulltext_hits


class FakeTsQueryBuilder:
    def __init__(self, query_text: str | None) -> None:
        self.query_text = query_text
        self.questions: list[str] = []

    def build(self, question: str) -> str | None:
        self.questions.append(question)
        return self.query_text


@dataclass
class FakeSettings:
    rag_vector_top_k: int = 20
    rag_fulltext_top_k: int = 10
    rag_min_score: float = 0.5
    rag_rrf_k: int = 60


def test_rrf_module_is_merged_into_hybrid_retriever() -> None:
    assert not Path("app/services/rrf.py").exists()


def test_rrf_scores_single_ranked_route() -> None:
    fused = _rrf_fuse({"vector": [_hit(10)]}, rrf_k=60)

    assert len(fused) == 1
    assert fused[0].hit.chunk_id == 10
    assert fused[0].score == 1 / 61
    assert fused[0].retrieval_sources == ("vector",)


def test_rrf_deduplicates_and_accumulates_scores_across_routes() -> None:
    fused = _rrf_fuse(
        {
            "vector": [_hit(10), _hit(11)],
            "fulltext": [_hit(11), _hit(12)],
        },
        rrf_k=60,
    )

    assert [item.hit.chunk_id for item in fused] == [11, 10, 12]
    assert fused[0].score == (1 / 62) + (1 / 61)
    assert fused[0].retrieval_sources == ("vector", "fulltext")


def test_rrf_keeps_first_seen_order_when_scores_tie() -> None:
    fused = _rrf_fuse(
        {
            "vector": [_hit(10), _hit(11)],
            "fulltext": [_hit(12), _hit(13)],
        },
        rrf_k=60,
    )

    assert [item.hit.chunk_id for item in fused] == [10, 12, 11, 13]


@pytest.mark.asyncio
async def test_retrieve_fuses_vector_and_fulltext_hits_with_rrf_scores() -> None:
    embedding = FakeEmbeddingService()
    repository = FakeChunkRepository(
        vector_hits=[_hit(10, 0.8), _hit(11, 0.7)],
        fulltext_hits=[_hit(11, 0.01), _hit(12, 0.01)],
    )
    builder = FakeTsQueryBuilder("Commit Message")
    retriever = HybridRetriever(
        embedding_service=embedding,
        chunk_repository=repository,
        ts_query_builder=builder,
        settings=FakeSettings(),
    )

    result = await retriever.retrieve(question=" Commit Message? ", kb_ids=[2])

    assert embedding.queries == ["Commit Message?"]
    assert builder.questions == ["Commit Message?"]
    assert repository.vector_calls == [{"query_vector": [0.1, 0.2], "kb_ids": [2], "top_k": 20}]
    assert repository.fulltext_calls == [{"query_text": "Commit Message", "kb_ids": [2], "top_k": 10}]
    assert [hit.chunk_id for hit in result.hits] == [11, 10, 12]
    assert result.hits[0].score == (1 / 62) + (1 / 61)
    assert result.vector_count == 2
    assert result.fulltext_count == 2


@pytest.mark.asyncio
async def test_retrieve_skips_fulltext_when_query_text_is_empty() -> None:
    repository = FakeChunkRepository(vector_hits=[_hit(10, 0.8)], fulltext_hits=[_hit(11, 0.01)])
    retriever = HybridRetriever(
        embedding_service=FakeEmbeddingService(),
        chunk_repository=repository,
        ts_query_builder=FakeTsQueryBuilder(None),
        settings=FakeSettings(),
    )

    result = await retriever.retrieve(question="what how", kb_ids=[2])

    assert repository.fulltext_calls == []
    assert [hit.chunk_id for hit in result.hits] == [10]
    assert result.fulltext_count == 0


@pytest.mark.asyncio
async def test_retrieve_keeps_low_vector_scores_before_rrf() -> None:
    repository = FakeChunkRepository(
        vector_hits=[_hit(10, 0.49), _hit(11, 0.75)],
        fulltext_hits=[_hit(10, 0.01)],
    )
    retriever = HybridRetriever(
        embedding_service=FakeEmbeddingService(),
        chunk_repository=repository,
        ts_query_builder=FakeTsQueryBuilder("policy"),
        settings=FakeSettings(rag_min_score=0.7),
    )

    result = await retriever.retrieve(question="policy", kb_ids=[2])

    assert [hit.chunk_id for hit in result.hits] == [10, 11]
    assert result.hits[0].score == (1 / 61) + (1 / 61)
