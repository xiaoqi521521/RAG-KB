from __future__ import annotations

from app.repositories.chunks import ChunkSearchHit
from app.services.enhanced_retriever import EnhancedRetriever
from app.services.hybrid_retriever import HybridRetrieveResult
from app.services.query_rewriter import HydeRewriteResult, MultiQueryRewriteResult


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


class FakeQueryRewriter:
    def __init__(self, hyde_answer: str | None, degraded_reasons: tuple[str, ...] = ()) -> None:
        self.hyde_answer = hyde_answer
        self.degraded_reasons = degraded_reasons
        self.hyde_calls: list[str] = []
        self.expand_calls: list[str] = []

    async def generate_hyde_answer(self, question: str) -> HydeRewriteResult:
        self.hyde_calls.append(question)
        return HydeRewriteResult(
            original_question=question.strip(),
            hyde_answer=self.hyde_answer,
            used_cache=False,
            degraded_reasons=self.degraded_reasons,
        )

    async def expand_queries(self, question: str) -> MultiQueryRewriteResult:
        self.expand_calls.append(question)
        return MultiQueryRewriteResult(
            original_question=question.strip(),
            expanded_queries=["不应调用"],
            used_cache=False,
            degraded_reasons=(),
        )


class FakeHybridRetriever:
    def __init__(self, hits: list[ChunkSearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict[str, object]] = []

    async def retrieve(self, *, question: str, kb_ids: list[int]) -> HybridRetrieveResult:
        self.calls.append({"question": question, "kb_ids": kb_ids})
        return HybridRetrieveResult(hits=self.hits, vector_count=len(self.hits), fulltext_count=0)


class FakeEmbeddingService:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        if self.fail:
            raise RuntimeError("embedding failed")
        return [0.1, 0.2]


class FakeChunkRepository:
    def __init__(self, vector_hits: list[ChunkSearchHit]) -> None:
        self.vector_hits = vector_hits
        self.vector_calls: list[dict[str, object]] = []

    async def search_by_vector(
        self,
        *,
        query_vector: list[float],
        kb_ids: list[int],
        top_k: int,
    ) -> list[ChunkSearchHit]:
        self.vector_calls.append({"query_vector": query_vector, "kb_ids": kb_ids, "top_k": top_k})
        return self.vector_hits


async def test_retrieve_fuses_original_hybrid_and_hyde_vector_results() -> None:
    rewriter = FakeQueryRewriter("员工申请年假需要在 OA 提交申请。")
    hybrid = FakeHybridRetriever([_hit(10), _hit(11)])
    embedding = FakeEmbeddingService()
    repository = FakeChunkRepository([_hit(11), _hit(12)])
    retriever = EnhancedRetriever(
        query_rewriter=rewriter,
        hybrid_retriever=hybrid,
        embedding_service=embedding,
        chunk_repository=repository,
        rrf_k=60,
        hyde_vector_top_k=20,
    )

    result = await retriever.retrieve(question=" 年假怎么申请？ ", kb_ids=[2])

    assert hybrid.calls == [{"question": "年假怎么申请？", "kb_ids": [2]}]
    assert rewriter.hyde_calls == ["年假怎么申请？"]
    assert rewriter.expand_calls == []
    assert embedding.queries == ["员工申请年假需要在 OA 提交申请。"]
    assert repository.vector_calls == [{"query_vector": [0.1, 0.2], "kb_ids": [2], "top_k": 20}]
    assert [hit.chunk_id for hit in result.hits] == [11, 10, 12]
    assert result.hits[0].score == (1 / 62) + (1 / 61)
    assert result.original_count == 2
    assert result.hyde_count == 2
    assert result.merged_count == 3


async def test_retrieve_degrades_to_original_hybrid_when_hyde_is_unavailable() -> None:
    rewriter = FakeQueryRewriter(None, degraded_reasons=("hyde_generation_failed",))
    hybrid = FakeHybridRetriever([_hit(10), _hit(11)])
    embedding = FakeEmbeddingService()
    repository = FakeChunkRepository([_hit(12)])
    retriever = EnhancedRetriever(
        query_rewriter=rewriter,
        hybrid_retriever=hybrid,
        embedding_service=embedding,
        chunk_repository=repository,
        rrf_k=60,
        hyde_vector_top_k=20,
    )

    result = await retriever.retrieve(question="年假怎么申请？", kb_ids=[2])

    assert [hit.chunk_id for hit in result.hits] == [10, 11]
    assert embedding.queries == []
    assert repository.vector_calls == []
    assert result.hyde_count == 0
    assert "hyde_generation_failed" in result.degraded_reasons


async def test_retrieve_degrades_to_original_hybrid_when_hyde_embedding_fails() -> None:
    retriever = EnhancedRetriever(
        query_rewriter=FakeQueryRewriter("员工申请年假需要在 OA 提交申请。"),
        hybrid_retriever=FakeHybridRetriever([_hit(10)]),
        embedding_service=FakeEmbeddingService(fail=True),
        chunk_repository=FakeChunkRepository([_hit(12)]),
        rrf_k=60,
        hyde_vector_top_k=20,
    )

    result = await retriever.retrieve(question="年假怎么申请？", kb_ids=[2])

    assert [hit.chunk_id for hit in result.hits] == [10]
    assert result.hyde_count == 0
    assert "hyde_retrieval_failed" in result.degraded_reasons
