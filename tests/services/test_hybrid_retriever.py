from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser, current_user_var
from app.repositories.chunks import ChunkSearchHit
from app.services.hybrid_retriever import HybridRetriever


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
        self.kb_scopes: list[str | int] = []

    async def embed_query(
        self,
        text: str,
        *,
        namespace: str = "query",
        cache_enabled: bool = False,
        kb_id: str | int = "unknown",
    ) -> list[float]:
        self.queries.append(text)
        self.kb_scopes.append(kb_id)
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


class FakePermissionService:
    def __init__(self, allowed_kb_ids: list[int], error: HTTPException | None = None) -> None:
        self.allowed_kb_ids = allowed_kb_ids
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def filter_readable_kb_ids(self, kb_ids: list[int], user: CurrentUser) -> list[int]:
        self.calls.append({"kb_ids": kb_ids, "user": user})
        if self.error is not None:
            raise self.error
        return [kb_id for kb_id in kb_ids if kb_id in self.allowed_kb_ids]


@dataclass
class FakeSettings:
    rag_vector_top_k: int = 20
    rag_fulltext_top_k: int = 10
    rag_min_score: float = 0.5
    rag_rrf_k: int = 60


def _user() -> CurrentUser:
    return CurrentUser(user_id=7, department_id="engineering", role="USER")


def _retriever(
    *,
    embedding: FakeEmbeddingService,
    repository: FakeChunkRepository,
    builder: FakeTsQueryBuilder,
    permission_service: FakePermissionService,
    settings: FakeSettings | None = None,
) -> HybridRetriever:
    return HybridRetriever(
        embedding_service=embedding,
        chunk_repository=repository,
        ts_query_builder=builder,
        settings=settings or FakeSettings(),
        permission_service=permission_service,
    )


@pytest.mark.asyncio
async def test_retrieve_fuses_vector_and_fulltext_hits_with_rrf_scores() -> None:
    user_token = current_user_var.set(_user())
    embedding = FakeEmbeddingService()
    repository = FakeChunkRepository(
        vector_hits=[_hit(10, 0.8), _hit(11, 0.7)],
        fulltext_hits=[_hit(11, 0.01), _hit(12, 0.01)],
    )
    builder = FakeTsQueryBuilder("Commit Message")
    try:
        retriever = _retriever(
            embedding=embedding,
            repository=repository,
            builder=builder,
            permission_service=FakePermissionService([2]),
        )

        result = await retriever.retrieve(question=" Commit Message? ", kb_ids=[2])

        assert embedding.queries == ["Commit Message?"]
        assert embedding.kb_scopes == ["2"]
        assert builder.questions == ["Commit Message?"]
        assert repository.vector_calls == [{"query_vector": [0.1, 0.2], "kb_ids": [2], "top_k": 20}]
        assert repository.fulltext_calls == [{"query_text": "Commit Message", "kb_ids": [2], "top_k": 10}]
        assert [hit.chunk_id for hit in result.hits] == [11, 10, 12]
        assert result.hits[0].score == (1 / 62) + (1 / 61)
        assert result.vector_count == 2
        assert result.fulltext_count == 2
        assert result.allowed_kb_ids == [2]
    finally:
        current_user_var.reset(user_token)


@pytest.mark.asyncio
async def test_retrieve_skips_fulltext_when_query_text_is_empty() -> None:
    user_token = current_user_var.set(_user())
    repository = FakeChunkRepository(vector_hits=[_hit(10, 0.8)], fulltext_hits=[_hit(11, 0.01)])
    try:
        retriever = _retriever(
            embedding=FakeEmbeddingService(),
            repository=repository,
            builder=FakeTsQueryBuilder(None),
            permission_service=FakePermissionService([2]),
        )

        result = await retriever.retrieve(question="what how", kb_ids=[2])

        assert repository.fulltext_calls == []
        assert [hit.chunk_id for hit in result.hits] == [10]
        assert result.fulltext_count == 0
    finally:
        current_user_var.reset(user_token)


@pytest.mark.asyncio
async def test_retrieve_keeps_low_vector_scores_before_rrf() -> None:
    user_token = current_user_var.set(_user())
    repository = FakeChunkRepository(
        vector_hits=[_hit(10, 0.49), _hit(11, 0.75)],
        fulltext_hits=[_hit(10, 0.01)],
    )
    try:
        retriever = _retriever(
            embedding=FakeEmbeddingService(),
            repository=repository,
            builder=FakeTsQueryBuilder("policy"),
            permission_service=FakePermissionService([2]),
            settings=FakeSettings(rag_min_score=0.7),
        )

        result = await retriever.retrieve(question="policy", kb_ids=[2])

        assert [hit.chunk_id for hit in result.hits] == [10, 11]
        assert result.hits[0].score == (1 / 61) + (1 / 61)
    finally:
        current_user_var.reset(user_token)


@pytest.mark.asyncio
async def test_retrieve_filters_denied_ids_before_embedding_and_both_repositories(
    caplog: pytest.LogCaptureFixture,
) -> None:
    user_token = current_user_var.set(_user())
    embedding = FakeEmbeddingService()
    repository = FakeChunkRepository(vector_hits=[_hit(10)], fulltext_hits=[_hit(11)])
    permission_service = FakePermissionService([2])
    try:
        retriever = _retriever(
            embedding=embedding,
            repository=repository,
            builder=FakeTsQueryBuilder("policy"),
            permission_service=permission_service,
        )

        with caplog.at_level("WARNING", logger="app.services.hybrid_retriever"):
            result = await retriever.retrieve(question="policy", kb_ids=[3, 2, 3, 2])

        assert permission_service.calls == [{"kb_ids": [3, 2], "user": _user()}]
        assert embedding.queries == ["policy"]
        assert repository.vector_calls[0]["kb_ids"] == [2]
        assert repository.fulltext_calls[0]["kb_ids"] == [2]
        assert result.allowed_kb_ids == [2]
        assert "user_id=7" in caplog.text
        assert "denied_kb_ids=[3]" in caplog.text
    finally:
        current_user_var.reset(user_token)


@pytest.mark.asyncio
async def test_retrieve_returns_forbidden_before_embedding_when_scope_is_empty() -> None:
    user_token = current_user_var.set(_user())
    embedding = FakeEmbeddingService()
    repository = FakeChunkRepository(vector_hits=[_hit(10)], fulltext_hits=[_hit(11)])
    try:
        retriever = _retriever(
            embedding=embedding,
            repository=repository,
            builder=FakeTsQueryBuilder("policy"),
            permission_service=FakePermissionService([]),
        )

        with pytest.raises(HTTPException) as exc_info:
            await retriever.retrieve(question="policy", kb_ids=[3])

        assert exc_info.value.status_code == 403
        assert embedding.queries == []
        assert repository.vector_calls == []
        assert repository.fulltext_calls == []
    finally:
        current_user_var.reset(user_token)


@pytest.mark.asyncio
async def test_retrieve_returns_unauthorized_without_current_user_context() -> None:
    user_token = current_user_var.set(None)
    permission_service = FakePermissionService([2])
    try:
        retriever = _retriever(
            embedding=FakeEmbeddingService(),
            repository=FakeChunkRepository(vector_hits=[], fulltext_hits=[]),
            builder=FakeTsQueryBuilder("policy"),
            permission_service=permission_service,
        )

        with pytest.raises(HTTPException) as exc_info:
            await retriever.retrieve(question="policy", kb_ids=[2])

        assert exc_info.value.status_code == 401
        assert permission_service.calls == []
    finally:
        current_user_var.reset(user_token)


@pytest.mark.asyncio
async def test_retrieve_preserves_permission_service_unavailable_error() -> None:
    user_token = current_user_var.set(_user())
    embedding = FakeEmbeddingService()
    try:
        retriever = _retriever(
            embedding=embedding,
            repository=FakeChunkRepository(vector_hits=[], fulltext_hits=[]),
            builder=FakeTsQueryBuilder("policy"),
            permission_service=FakePermissionService(
                [],
                error=HTTPException(status_code=503, detail="权限服务暂不可用"),
            ),
        )

        with pytest.raises(HTTPException) as exc_info:
            await retriever.retrieve(question="policy", kb_ids=[2])

        assert exc_info.value.status_code == 503
        assert embedding.queries == []
    finally:
        current_user_var.reset(user_token)
