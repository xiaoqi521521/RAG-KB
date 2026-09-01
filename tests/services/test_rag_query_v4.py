from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.repositories.chunks import ChunkSearchHit
from app.schemas.rag import SourceCitation
from app.services.enhanced_retriever import EnhancedRetrieveResult
from app.services.faithfulness_evaluator import FaithfulnessResult, FaithfulnessStatus
from app.services.rag_query import RAG_REFUSAL_ANSWER
from app.services.reranker import RerankResult
from app.services.rag_query_v4 import RagQueryServiceV4
from app.services.source_builder import (
    BuiltSourceContext,
    CitationSelectionResult,
    CitationSelectionStatus,
    SourceBuilder,
)


def _user() -> CurrentUser:
    return CurrentUser(user_id=1, department_id="engineering", role="ADMIN")


def _hit(chunk_id: int, score: float = 0.1) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=1,
        document_name="dev-guide.md",
        kb_id=2,
        chunk_index=chunk_id,
        content=f"chunk {chunk_id} content",
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
            hyde_count=1,
            merged_count=len(self.hits),
            degraded_reasons=(),
        )


class FakeReranker:
    def __init__(self, result: RerankResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def rerank(self, *, question: str, candidates: list[ChunkSearchHit]) -> RerankResult:
        self.calls.append({"question": question, "candidates": candidates})
        return self.result


class FakeConfidenceFilter:
    def __init__(self, hits: list[ChunkSearchHit]) -> None:
        self.hits = hits
        self.calls: list[list[ChunkSearchHit]] = []

    def filter(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        self.calls.append(hits)
        return self.hits


class FakeContextTrimmer:
    def __init__(self, hits: list[ChunkSearchHit]) -> None:
        self.hits = hits
        self.calls: list[list[ChunkSearchHit]] = []

    async def trim(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        self.calls.append(hits)
        return self.hits


class FakeSourceBuilder:
    def __init__(self, *, context: str = "[参考1]\nchunk 12 content") -> None:
        self.context = context
        self.calls: list[dict[str, object]] = []

    def build(
        self,
        hits: list[ChunkSearchHit],
        *,
        return_top_n: int,
    ) -> tuple[str, list[SourceCitation]]:
        self.calls.append({"hits": hits, "return_top_n": return_top_n})
        if not self.context:
            return "", []
        return (
            self.context,
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
                for reference_index, hit in enumerate(hits[:return_top_n], start=1)
            ],
        )

    def build_context(
        self,
        hits: list[ChunkSearchHit],
        *,
        return_top_n: int,
    ) -> BuiltSourceContext:
        context, sources = self.build(hits, return_top_n=return_top_n)
        return BuiltSourceContext(
            context=context,
            sources=sources,
            reference_contexts=[hit.content for hit in hits[: len(sources)]],
        )

    def resolve_citations(
        self,
        answer: str,
        available_sources: list[SourceCitation],
    ) -> CitationSelectionResult:
        return CitationSelectionResult(
            status=CitationSelectionStatus.FALLBACK_ALL,
            sources=available_sources,
            referenced_count=0,
            valid_count=0,
            invalid_count=0,
            answer=answer,
        )


class FakeChatModel:
    def __init__(
        self,
        content: object = "需要先运行测试。[参考1]",
        completion_tokens: int | None = None,
    ) -> None:
        self.content = content
        self.completion_tokens = completion_tokens
        self.messages: list[object] | None = None
        self.call_count = 0

    async def ainvoke(self, messages: list[object]) -> SimpleNamespace:
        self.call_count += 1
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


class FakeFaithfulnessEvaluator:
    def __init__(self, result: FaithfulnessResult) -> None:
        self.result = result
        self.calls: list[dict[str, str]] = []

    async def evaluate(
        self,
        *,
        question: str,
        answer: str,
        context: str,
        kb_id: str | int = "unknown",
    ) -> FaithfulnessResult:
        self.calls.append({"question": question, "answer": answer, "context": context})
        return self.result


@dataclass
class FakeSettings:
    rag_return_top_n: int = 5


def _rerank_result(*, hits: list[ChunkSearchHit], degraded: bool = False) -> RerankResult:
    return RerankResult(
        hits=hits,
        degraded=degraded,
        degraded_reason="reranker_timeout" if degraded else None,
        input_count=3,
        output_count=len(hits),
        elapsed_ms=8,
        total_tokens=123,
    )


def _service(
    *,
    retrieve_hits: list[ChunkSearchHit],
    rerank_result: RerankResult,
    filtered_hits: list[ChunkSearchHit] | None = None,
    trimmed_hits: list[ChunkSearchHit] | None = None,
    source_context: str = "[参考1]\nchunk 12 content",
    chat_content: object = "需要先运行测试。[参考1]",
    completion_tokens: int | None = None,
    return_top_n: int = 5,
    faithfulness_evaluator: FakeFaithfulnessEvaluator | None = None,
) -> tuple[
    RagQueryServiceV4,
    FakeRetriever,
    FakeReranker,
    FakeConfidenceFilter,
    FakeContextTrimmer,
    FakeSourceBuilder,
    FakeChatModel,
]:
    retriever = FakeRetriever(retrieve_hits)
    reranker = FakeReranker(rerank_result)
    confidence_filter = FakeConfidenceFilter(
        filtered_hits if filtered_hits is not None else rerank_result.hits
    )
    context_trimmer = FakeContextTrimmer(
        trimmed_hits if trimmed_hits is not None else confidence_filter.hits
    )
    source_builder = FakeSourceBuilder(context=source_context)
    chat = FakeChatModel(chat_content, completion_tokens)
    token_metrics = FakeTokenMetrics()
    service = RagQueryServiceV4(
        retriever=retriever,
        reranker=reranker,
        confidence_filter=confidence_filter,
        context_trimmer=context_trimmer,
        source_builder=source_builder,
        chat_model=chat,
        token_metrics=token_metrics,
        settings=FakeSettings(rag_return_top_n=return_top_n),
        faithfulness_evaluator=faithfulness_evaluator,
    )
    return service, retriever, reranker, confidence_filter, context_trimmer, source_builder, chat


@pytest.mark.asyncio
async def test_query_records_generation_tokens_from_model_usage() -> None:
    service, _, _, _, _, _, _ = _service(
        retrieve_hits=[_hit(10), _hit(11), _hit(12)],
        rerank_result=_rerank_result(hits=[_hit(12, 0.91)]),
        completion_tokens=88,
    )

    await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert service.token_metrics.generation_tokens == [88]


@pytest.mark.asyncio
async def test_query_reranks_filters_trims_then_generates_answer() -> None:
    retrieve_hits = [_hit(10), _hit(11), _hit(12)]
    reranked_hits = [_hit(12, 0.91), _hit(10, 0.82)]
    filtered_hits = [_hit(12, 0.91)]
    service, retriever, reranker, confidence_filter, context_trimmer, source_builder, chat = (
        _service(
            retrieve_hits=retrieve_hits,
            rerank_result=_rerank_result(hits=reranked_hits),
            filtered_hits=filtered_hits,
        )
    )

    response = await service.query(question=" commit rule? ", kb_ids=[2], user=_user())

    assert response.answer == "需要先运行测试。[参考1]"
    assert [source.chunk_id for source in response.sources] == [12]
    assert retriever.calls == [{"question": "commit rule?", "kb_ids": [2]}]
    assert reranker.calls == [{"question": "commit rule?", "candidates": retrieve_hits}]
    assert confidence_filter.calls == [reranked_hits]
    assert context_trimmer.calls == [filtered_hits]
    assert source_builder.calls[0]["hits"] == filtered_hits
    assert chat.messages is not None
    assert chat.messages[1].content == "commit rule?"


@pytest.mark.asyncio
async def test_query_limits_candidates_before_context_token_trimming() -> None:
    retrieve_hits = [_hit(10), _hit(11), _hit(12)]
    reranked_hits = [_hit(12, 0.91), _hit(11, 0.86), _hit(10, 0.82)]
    service, _, _, _, context_trimmer, source_builder, _ = _service(
        retrieve_hits=retrieve_hits,
        rerank_result=_rerank_result(hits=reranked_hits),
        trimmed_hits=reranked_hits[:2],
        return_top_n=2,
    )

    await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert context_trimmer.calls == [reranked_hits[:2]]
    assert source_builder.calls[0]["hits"] == reranked_hits[:2]


@pytest.mark.asyncio
async def test_query_skips_confidence_filter_when_reranker_degraded() -> None:
    retrieve_hits = [_hit(10, 0.03), _hit(11, 0.02), _hit(12, 0.01)]
    degraded_hits = retrieve_hits[:2]
    service, _, _, confidence_filter, context_trimmer, source_builder, _ = _service(
        retrieve_hits=retrieve_hits,
        rerank_result=_rerank_result(hits=degraded_hits, degraded=True),
    )

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert [source.chunk_id for source in response.sources] == [10, 11]
    assert confidence_filter.calls == []
    assert context_trimmer.calls == [degraded_hits]
    assert source_builder.calls[0]["hits"] == degraded_hits


@pytest.mark.asyncio
async def test_query_skips_confidence_filter_when_reranker_was_not_called_for_few_candidates() -> (
    None
):
    retrieve_hits = [_hit(10, 0.03), _hit(11, 0.02)]
    skipped_result = RerankResult(
        hits=retrieve_hits,
        degraded=False,
        degraded_reason="skipped_not_enough_candidates",
        input_count=2,
        output_count=2,
        elapsed_ms=0,
        total_tokens=None,
    )
    service, _, _, confidence_filter, context_trimmer, source_builder, _ = _service(
        retrieve_hits=retrieve_hits,
        rerank_result=skipped_result,
        filtered_hits=[_hit(10, 0.03)],
        trimmed_hits=retrieve_hits,
    )

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert [source.chunk_id for source in response.sources] == [10, 11]
    assert confidence_filter.calls == []
    assert context_trimmer.calls == [retrieve_hits]
    assert source_builder.calls[0]["hits"] == retrieve_hits


@pytest.mark.asyncio
async def test_query_returns_refusal_without_reranking_when_retriever_has_no_hits() -> None:
    service, _, reranker, confidence_filter, context_trimmer, source_builder, chat = _service(
        retrieve_hits=[],
        rerank_result=_rerank_result(hits=[]),
    )

    response = await service.query(question="unknown?", kb_ids=[2], user=_user())

    assert response.answer == RAG_REFUSAL_ANSWER
    assert response.sources == []
    assert reranker.calls == []
    assert confidence_filter.calls == []
    assert context_trimmer.calls == []
    assert source_builder.calls == []
    assert chat.messages is None


@pytest.mark.asyncio
async def test_query_returns_refusal_when_context_trimmer_returns_no_hits() -> None:
    service, _, _, _, context_trimmer, source_builder, chat = _service(
        retrieve_hits=[_hit(10), _hit(11), _hit(12)],
        rerank_result=_rerank_result(hits=[_hit(12, 0.91)]),
        trimmed_hits=[],
    )

    response = await service.query(question="unknown?", kb_ids=[2], user=_user())

    assert response.answer == RAG_REFUSAL_ANSWER
    assert response.sources == []
    assert context_trimmer.calls == [[_hit(12, 0.91)]]
    assert source_builder.calls == []
    assert chat.messages is None


@pytest.mark.asyncio
async def test_query_returns_refusal_when_source_builder_returns_empty_context() -> None:
    service, _, _, _, _, _, chat = _service(
        retrieve_hits=[_hit(10), _hit(11), _hit(12)],
        rerank_result=_rerank_result(hits=[_hit(12, 0.91)]),
        source_context="",
    )

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert response.answer == RAG_REFUSAL_ANSWER
    assert response.sources == []
    assert chat.messages is None


@pytest.mark.asyncio
async def test_query_raises_503_when_chat_returns_blank_answer() -> None:
    service, _, _, _, _, _, _ = _service(
        retrieve_hits=[_hit(10), _hit(11), _hit(12)],
        rerank_result=_rerank_result(hits=[_hit(12, 0.91)]),
        chat_content="   ",
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.query(question="commit rule?", kb_ids=[2], user=_user())

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_query_uses_v4_prompt_and_returns_only_answer_citations() -> None:
    retrieve_hits = [_hit(10), _hit(11), _hit(12)]
    reranked_hits = [_hit(10, 0.91), _hit(11, 0.86)]
    retriever = FakeRetriever(retrieve_hits)
    reranker = FakeReranker(_rerank_result(hits=reranked_hits))
    confidence_filter = FakeConfidenceFilter(reranked_hits)
    context_trimmer = FakeContextTrimmer(reranked_hits)
    chat = FakeChatModel("第二条内容有效（来源：[参考2]）。")
    service = RagQueryServiceV4(
        retriever=retriever,
        reranker=reranker,
        confidence_filter=confidence_filter,
        context_trimmer=context_trimmer,
        source_builder=SourceBuilder(max_context_chars=1000),
        chat_model=chat,
        token_metrics=FakeTokenMetrics(),
        settings=FakeSettings(rag_return_top_n=2),
    )

    response = await service.query(question="哪条内容有效？", kb_ids=[2], user=_user())

    assert [source.chunk_id for source in response.sources] == [11]
    assert response.sources[0].reference_index == 1
    assert response.answer == "第二条内容有效（来源：[参考1]）。"
    assert response.hit_count == 1
    assert chat.messages is not None
    assert "每条事实后都要标注来源" in chat.messages[0].content


@pytest.mark.asyncio
async def test_query_returns_empty_sources_when_all_answer_citations_are_invalid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hits = [_hit(10, 0.91), _hit(11, 0.86)]
    chat = FakeChatModel("原回答保持可用（来源：[参考99]）。")
    service = RagQueryServiceV4(
        retriever=FakeRetriever(hits),
        reranker=FakeReranker(_rerank_result(hits=hits)),
        confidence_filter=FakeConfidenceFilter(hits),
        context_trimmer=FakeContextTrimmer(hits),
        source_builder=SourceBuilder(max_context_chars=1000),
        chat_model=chat,
        token_metrics=FakeTokenMetrics(),
        settings=FakeSettings(rag_return_top_n=2),
    )

    with caplog.at_level("INFO", logger="app.services.rag_query_v4"):
        response = await service.query(question="哪条内容有效？", kb_ids=[2], user=_user())

    assert response.answer == "原回答保持可用（来源：[参考99]）。"
    assert response.sources == []
    assert response.hit_count == 0
    assert "citation_status=invalid" in caplog.text
    assert "valid_count=0" in caplog.text
    assert "invalid_count=1" in caplog.text


@pytest.mark.asyncio
async def test_query_normalizes_explicit_model_refusal_to_fixed_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hits = [_hit(10, 0.91)]
    service = RagQueryServiceV4(
        retriever=FakeRetriever(hits),
        reranker=FakeReranker(_rerank_result(hits=hits)),
        confidence_filter=FakeConfidenceFilter(hits),
        context_trimmer=FakeContextTrimmer(hits),
        source_builder=SourceBuilder(max_context_chars=1000),
        chat_model=FakeChatModel("抱歉，在知识库中未找到相关内容。"),
        token_metrics=FakeTokenMetrics(),
        settings=FakeSettings(rag_return_top_n=1),
    )

    with caplog.at_level("INFO", logger="app.services.rag_query_v4"):
        response = await service.query(question="未知制度？", kb_ids=[2], user=_user())

    assert response.answer == RAG_REFUSAL_ANSWER
    assert response.sources == []
    assert response.hit_count == 0
    assert "citation_status=refusal" in caplog.text


@pytest.mark.asyncio
async def test_query_observes_unfaithful_answer_without_changing_public_response() -> None:
    evaluator = FakeFaithfulnessEvaluator(
        FaithfulnessResult(
            status=FaithfulnessStatus.UNFAITHFUL,
            score=0.2,
            reason="参考内容没有支持该结论",
            elapsed_ms=4,
            sampled=True,
        )
    )
    service, _, _, _, _, _, _ = _service(
        retrieve_hits=[_hit(10)],
        rerank_result=_rerank_result(hits=[_hit(10, 0.91)]),
        faithfulness_evaluator=evaluator,
    )

    response = await service.query(question="commit rule?", kb_ids=[2], user=_user())
    await asyncio.sleep(0)

    assert response.answer == "需要先运行测试。[参考1]"
    assert [source.chunk_id for source in response.sources] == [10]
    assert response.hit_count == 1
    assert evaluator.calls == [
        {
            "question": "commit rule?",
            "answer": "需要先运行测试。[参考1]",
            "context": "[参考1]\nchunk 12 content",
        }
    ]


@pytest.mark.asyncio
async def test_query_returns_before_faithfulness_observation_completes() -> None:
    class BlockingEvaluator:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def evaluate(
            self,
            *,
            question: str,
            answer: str,
            context: str,
            kb_id: str | int = "unknown",
        ) -> FaithfulnessResult:
            self.started.set()
            await self.release.wait()
            return FaithfulnessResult(
                status=FaithfulnessStatus.FAITHFUL,
                score=1.0,
                reason=None,
                elapsed_ms=1,
                sampled=True,
            )

    evaluator = BlockingEvaluator()
    service, _, _, _, _, _, _ = _service(
        retrieve_hits=[_hit(10)],
        rerank_result=_rerank_result(hits=[_hit(10, 0.91)]),
        faithfulness_evaluator=evaluator,  # type: ignore[arg-type]
    )

    response = await asyncio.wait_for(
        service.query(question="commit rule?", kb_ids=[2], user=_user()),
        timeout=0.1,
    )
    await asyncio.wait_for(evaluator.started.wait(), timeout=0.1)
    evaluator.release.set()
    await asyncio.sleep(0)

    assert response.answer == "需要先运行测试。[参考1]"


@pytest.mark.asyncio
async def test_query_does_not_observe_explicit_refusal() -> None:
    evaluator = FakeFaithfulnessEvaluator(
        FaithfulnessResult(
            status=FaithfulnessStatus.FAITHFUL,
            score=1.0,
            reason=None,
            elapsed_ms=4,
            sampled=True,
        )
    )
    service, _, _, _, _, _, _ = _service(
        retrieve_hits=[_hit(10)],
        rerank_result=_rerank_result(hits=[_hit(10, 0.91)]),
        chat_content="在知识库中未找到相关内容。",
        faithfulness_evaluator=evaluator,
    )

    response = await service.query(question="unknown?", kb_ids=[2], user=_user())

    assert response.answer == RAG_REFUSAL_ANSWER
    assert evaluator.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected_reference_indices"),
    [
        ("需要先完成审批流程。", [1, 2]),
        ("审批流程见制度（来源：[参考1]），但办理时限未找到相关内容。", [1]),
    ],
)
async def test_query_preserves_normal_answers_with_fallback_or_partial_missing_information(
    answer: str,
    expected_reference_indices: list[int],
) -> None:
    hits = [_hit(10, 0.91), _hit(11, 0.86)]
    retriever = FakeRetriever(hits)
    chat = FakeChatModel(answer)
    service = RagQueryServiceV4(
        retriever=retriever,
        reranker=FakeReranker(_rerank_result(hits=hits)),
        confidence_filter=FakeConfidenceFilter(hits),
        context_trimmer=FakeContextTrimmer(hits),
        source_builder=SourceBuilder(max_context_chars=1000),
        chat_model=chat,
        token_metrics=FakeTokenMetrics(),
        settings=FakeSettings(rag_return_top_n=2),
    )

    response = await service.query(question="审批规则？", kb_ids=[2], user=_user())

    assert response.answer == answer
    assert [source.reference_index for source in response.sources] == expected_reference_indices
    assert response.hit_count == len(expected_reference_indices)
    assert len(retriever.calls) == 1
    assert chat.call_count == 1


@pytest.mark.asyncio
async def test_execute_returns_public_response_and_internal_rag_evidence_once() -> None:
    reranked_hits = [_hit(12, 0.91), _hit(11, 0.86), _hit(10, 0.82)]
    evaluator = FakeFaithfulnessEvaluator(
        FaithfulnessResult(
            status=FaithfulnessStatus.FAITHFUL,
            score=1.0,
            reason=None,
            elapsed_ms=1,
            sampled=True,
        )
    )
    retriever = FakeRetriever([_hit(10), _hit(11), _hit(12)])
    reranker = FakeReranker(_rerank_result(hits=reranked_hits))
    chat = FakeChatModel("需要先运行测试。[参考1]")
    service = RagQueryServiceV4(
        retriever=retriever,
        reranker=reranker,
        confidence_filter=FakeConfidenceFilter(reranked_hits),
        context_trimmer=FakeContextTrimmer(reranked_hits[:2]),
        source_builder=SourceBuilder(max_context_chars=1000),
        chat_model=chat,
        token_metrics=FakeTokenMetrics(),
        settings=FakeSettings(rag_return_top_n=2),
        faithfulness_evaluator=evaluator,
    )

    execution = await service.execute(question="commit rule?", kb_ids=[2], user=_user())
    await asyncio.sleep(0)

    assert execution.public_response.answer == "需要先运行测试。[参考1]"
    assert [source.chunk_id for source in execution.public_response.sources] == [12]
    assert execution.reranked_hits == reranked_hits
    assert execution.reference_contexts == ["chunk 12 content", "chunk 11 content"]
    assert execution.reranker_degraded is False
    assert execution.degraded_reason is None
    assert execution.explicit_refusal is False
    assert len(retriever.calls) == 1
    assert len(reranker.calls) == 1
    assert chat.call_count == 1
    assert evaluator.calls == []


@pytest.mark.asyncio
async def test_execute_preserves_reranker_degradation_for_formal_evaluation() -> None:
    degraded_hits = [_hit(10, 0.03), _hit(11, 0.02)]
    service, _, _, _, _, _, _ = _service(
        retrieve_hits=[*degraded_hits, _hit(12, 0.01)],
        rerank_result=_rerank_result(hits=degraded_hits, degraded=True),
    )

    execution = await service.execute(question="commit rule?", kb_ids=[2], user=_user())

    assert execution.public_response.answer == "需要先运行测试。[参考1]"
    assert execution.reranked_hits == degraded_hits
    assert execution.reranker_degraded is True
    assert execution.degraded_reason == "reranker_timeout"
    assert execution.explicit_refusal is False


@pytest.mark.asyncio
async def test_execute_exposes_only_reference_text_injected_after_character_truncation() -> None:
    full_content = "制度正文" * 100
    hit = replace(_hit(10, 0.91), content=full_content)
    service = RagQueryServiceV4(
        retriever=FakeRetriever([hit]),
        reranker=FakeReranker(_rerank_result(hits=[hit])),
        confidence_filter=FakeConfidenceFilter([hit]),
        context_trimmer=FakeContextTrimmer([hit]),
        source_builder=SourceBuilder(max_context_chars=120),
        chat_model=FakeChatModel("按制度执行。[参考1]"),
        token_metrics=FakeTokenMetrics(),
        settings=FakeSettings(rag_return_top_n=1),
    )

    execution = await service.execute(question="如何执行？", kb_ids=[2], user=_user())

    assert len(execution.reference_contexts) == 1
    assert 0 < len(execution.reference_contexts[0]) < len(full_content)
    assert execution.reference_contexts[0] in execution.prompt_context
    assert full_content not in execution.prompt_context


@pytest.mark.asyncio
async def test_execute_preserves_internal_evidence_when_model_explicitly_refuses() -> None:
    hits = [_hit(10, 0.91)]
    evaluator = FakeFaithfulnessEvaluator(
        FaithfulnessResult(
            status=FaithfulnessStatus.FAITHFUL,
            score=1.0,
            reason=None,
            elapsed_ms=1,
            sampled=True,
        )
    )
    service = RagQueryServiceV4(
        retriever=FakeRetriever(hits),
        reranker=FakeReranker(_rerank_result(hits=hits)),
        confidence_filter=FakeConfidenceFilter(hits),
        context_trimmer=FakeContextTrimmer(hits),
        source_builder=SourceBuilder(max_context_chars=1000),
        chat_model=FakeChatModel("在知识库中未找到相关内容。"),
        token_metrics=FakeTokenMetrics(),
        settings=FakeSettings(rag_return_top_n=1),
        faithfulness_evaluator=evaluator,
    )

    execution = await service.execute(question="未知制度？", kb_ids=[2], user=_user())
    await asyncio.sleep(0)

    assert execution.public_response.answer == RAG_REFUSAL_ANSWER
    assert execution.public_response.sources == []
    assert execution.reranked_hits == hits
    assert execution.reference_contexts == ["chunk 10 content"]
    assert execution.prompt_context
    assert execution.explicit_refusal is True
    assert evaluator.calls == []


@pytest.mark.asyncio
async def test_execute_preserves_rerank_diagnostics_when_context_trimming_refuses() -> None:
    reranked_hits = [_hit(10, 0.03), _hit(11, 0.02)]
    service, _, _, _, _, _, chat = _service(
        retrieve_hits=[*reranked_hits, _hit(12, 0.01)],
        rerank_result=_rerank_result(hits=reranked_hits, degraded=True),
        trimmed_hits=[],
    )

    execution = await service.execute(question="未知制度？", kb_ids=[2], user=_user())

    assert execution.public_response.answer == RAG_REFUSAL_ANSWER
    assert execution.reranked_hits == reranked_hits
    assert execution.reference_contexts == []
    assert execution.prompt_context == ""
    assert execution.reranker_degraded is True
    assert execution.degraded_reason == "reranker_timeout"
    assert execution.explicit_refusal is True
    assert chat.messages is None
