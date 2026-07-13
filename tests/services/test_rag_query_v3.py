from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.repositories.chunks import ChunkSearchHit
from app.schemas.rag import SourceCitation
from app.services.enhanced_retriever import EnhancedRetrieveResult
from app.services.rag_query import RAG_REFUSAL_ANSWER
from app.services.rag_query_v3 import RagQueryServiceV3


def _user() -> CurrentUser:
    return CurrentUser(user_id=1, department_id="engineering", role="ADMIN")


def _hit(chunk_id: int = 10, score: float = 0.9) -> ChunkSearchHit:
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

    async def retrieve(self, *, question: str, kb_ids: list[int]) -> EnhancedRetrieveResult:
        self.calls.append({"question": question, "kb_ids": kb_ids})
        return EnhancedRetrieveResult(
            hits=self.hits,
            original_count=len(self.hits),
            hyde_count=0,
            merged_count=len(self.hits),
            degraded_reasons=(),
        )


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
            "[参考1]\nRun local tests before committing code.",
            [
                SourceCitation(
                    reference_index=reference_index,
                    document_id=hit.doc_id,
                    document_name=hit.document_name,
                    kb_id=hit.kb_id,
                    chunk_id=hit.chunk_id,
                    chunk_index=hit.chunk_index,
                    page_number=hit.page_num,
                    section_title=hit.section_title,
                    excerpt=hit.content[:200],
                    score=hit.score,
                )
                for reference_index, hit in enumerate(selected_hits, start=1)
            ],
        )


class FakeChatModel:
    def __init__(
        self,
        content: object = "Run local tests before committing code. [参考1]",
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


def _service(
    *,
    hits: list[ChunkSearchHit],
    chat_content: object = "Run local tests before committing code. [参考1]",
    completion_tokens: int | None = None,
    return_top_n: int = 5,
) -> tuple[RagQueryServiceV3, FakeRetriever, FakeSourceBuilder, FakeChatModel]:
    retriever = FakeRetriever(hits)
    source_builder = FakeSourceBuilder()
    chat = FakeChatModel(chat_content, completion_tokens)
    token_metrics = FakeTokenMetrics()
    service = RagQueryServiceV3(
        retriever=retriever,
        source_builder=source_builder,
        chat_model=chat,
        token_metrics=token_metrics,
        settings=FakeSettings(rag_return_top_n=return_top_n),
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
async def test_query_generates_answer_with_context_and_sources() -> None:
    service, _, source_builder, chat = _service(hits=[_hit(score=0.9)])

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert response.answer == "Run local tests before committing code. [参考1]"
    assert response.sources[0].chunk_id == 10
    assert response.hit_count == 1
    assert source_builder.calls[0]["return_top_n"] == 5
    assert chat.messages is not None
    assert "[参考1]" in chat.messages[0].content
    assert chat.messages[1].content == "commit rule?"


@pytest.mark.asyncio
async def test_query_hit_count_uses_sources_injected_into_prompt() -> None:
    service, _, _, _ = _service(hits=[_hit(10), _hit(11)], return_top_n=1)

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert response.hit_count == 1
    assert [source.chunk_id for source in response.sources] == [10]


@pytest.mark.asyncio
async def test_query_raises_503_when_chat_returns_blank_answer() -> None:
    service, _, _, _ = _service(hits=[_hit()], chat_content="   ")

    with pytest.raises(HTTPException) as exc_info:
        await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert exc_info.value.status_code == 503
