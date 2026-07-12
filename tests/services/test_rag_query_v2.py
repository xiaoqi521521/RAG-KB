from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.repositories.chunks import ChunkSearchHit
from app.schemas.rag import SourceCitation
from app.services.hybrid_retriever import HybridRetrieveResult
from app.services.rag_query_v2 import RAG_REFUSAL_ANSWER, RagQueryServiceV2


def _user() -> CurrentUser:
    return CurrentUser(user_id=1, department_id="engineering", role="ADMIN")


def _hit(*, chunk_id: int = 10, score: float = 0.9) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=1,
        document_name="dev-guide.md",
        kb_id=2,
        chunk_index=3,
        content="Run local tests before committing code.",
        page_num=None,
        section_title="Commit",
        score=score,
    )


class FakeRetriever:
    def __init__(self, hits: list[ChunkSearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict[str, object]] = []

    async def retrieve(
        self,
        *,
        question: str,
        kb_ids: list[int],
    ) -> HybridRetrieveResult:
        self.calls.append({"question": question, "kb_ids": kb_ids})
        return HybridRetrieveResult(hits=self.hits, vector_count=len(self.hits), fulltext_count=0)


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
        selected_hits = hits[:return_top_n]
        return (
            "[ref1]\nRun local tests before committing code.",
            [
                SourceCitation(
                    document_id=1,
                    document_name="dev-guide.md",
                    kb_id=2,
                    chunk_id=hit.chunk_id,
                    chunk_index=3,
                    page_number=None,
                    section_title="Commit",
                    score=hit.score,
                )
                for hit in selected_hits
            ],
        )


class FakeChatModel:
    def __init__(
        self,
        content: object = "Run local tests before committing code. [ref1]",
        completion_tokens: int | None = None,
    ) -> None:
        self.content = content
        self.completion_tokens = completion_tokens
        self.messages: list[object] | None = None

    async def ainvoke(self, messages: list[object]) -> SimpleNamespace:
        self.messages = messages
        usage_metadata = (
            {"output_tokens": self.completion_tokens}
            if self.completion_tokens is not None
            else None
        )
        return SimpleNamespace(content=self.content, usage_metadata=usage_metadata)


class FakeTokenMetrics:
    def __init__(self) -> None:
        self.generation_tokens: list[int] = []

    async def record_generation_tokens(self, *, tokens: int, source: str = "provider") -> None:
        self.generation_tokens.append(tokens)


@dataclass
class FakeSettings:
    rag_return_top_n: int = 5
    rag_min_score: float = 0.5


def _service(
    *,
    hits: list[ChunkSearchHit],
    chat_content: object = "Run local tests before committing code. [ref1]",
    completion_tokens: int | None = None,
    min_score: float = 0.5,
    return_top_n: int = 5,
) -> tuple[RagQueryServiceV2, FakeRetriever, FakeSourceBuilder, FakeChatModel]:
    retriever = FakeRetriever(hits)
    source_builder = FakeSourceBuilder()
    chat = FakeChatModel(chat_content, completion_tokens)
    token_metrics = FakeTokenMetrics()
    service = RagQueryServiceV2(
        retriever=retriever,
        source_builder=source_builder,
        chat_model=chat,
        token_metrics=token_metrics,
        settings=FakeSettings(rag_min_score=min_score, rag_return_top_n=return_top_n),
    )
    return service, retriever, source_builder, chat


@pytest.mark.asyncio
async def test_query_records_generation_tokens_from_model_usage() -> None:
    service, _, _, _ = _service(hits=[_hit()], completion_tokens=88)

    await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert service.token_metrics.generation_tokens == [88]


@pytest.mark.asyncio
async def test_query_returns_refusal_without_calling_chat_when_no_hits() -> None:
    service, retriever, source_builder, chat = _service(hits=[])

    response = await service.query(question=" quantum policy? ", kb_ids=[2], user=_user())

    assert response.answer == RAG_REFUSAL_ANSWER
    assert response.sources == []
    assert response.hit_count == 0
    assert retriever.calls == [{"question": "quantum policy?", "kb_ids": [2]}]
    assert source_builder.calls == []
    assert chat.messages is None


@pytest.mark.asyncio
async def test_query_does_not_apply_similarity_threshold_to_hybrid_scores() -> None:
    service, _, source_builder, chat = _service(hits=[_hit(score=0.3)])

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert response.answer == "Run local tests before committing code. [ref1]"
    assert response.sources[0].score == 0.3
    assert response.hit_count == 1
    assert source_builder.calls[0]["hits"][0].chunk_id == 10
    assert chat.messages is not None


@pytest.mark.asyncio
async def test_query_generates_answer_with_context_and_sources() -> None:
    service, _, source_builder, chat = _service(hits=[_hit(score=0.9)])

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert response.answer == "Run local tests before committing code. [ref1]"
    assert response.sources[0].chunk_id == 10
    assert response.hit_count == 1
    assert response.latency_ms >= 0
    assert source_builder.calls[0]["return_top_n"] == 5
    assert chat.messages is not None
    assert "[ref1]" in chat.messages[0].content
    assert chat.messages[1].content == "commit rule?"


@pytest.mark.asyncio
async def test_query_uses_configured_return_top_n() -> None:
    service, _, source_builder, _ = _service(
        hits=[_hit(chunk_id=10)],
        return_top_n=2,
    )

    await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert source_builder.calls[0]["return_top_n"] == 2


@pytest.mark.asyncio
async def test_query_hit_count_uses_sources_injected_into_prompt() -> None:
    service, _, _, _ = _service(
        hits=[_hit(chunk_id=10), _hit(chunk_id=11)],
        return_top_n=1,
    )

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert response.hit_count == 1
    assert [source.chunk_id for source in response.sources] == [10]


@pytest.mark.asyncio
async def test_query_passes_all_hybrid_hits_to_source_builder() -> None:
    service, _, source_builder, _ = _service(
        hits=[_hit(chunk_id=10, score=0.76), _hit(chunk_id=11, score=0.68)],
        min_score=0.7,
    )

    response = await service.query(question="working hours?", kb_ids=[2], user=_user())

    passed_hits = source_builder.calls[0]["hits"]
    assert [hit.chunk_id for hit in passed_hits] == [10, 11]
    assert response.hit_count == 2
    assert [source.chunk_id for source in response.sources] == [10, 11]


@pytest.mark.asyncio
async def test_query_raises_503_when_chat_returns_blank_answer() -> None:
    service, _, _, _ = _service(hits=[_hit(score=0.9)], chat_content="   ")

    with pytest.raises(HTTPException) as exc_info:
        await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert exc_info.value.status_code == 503
