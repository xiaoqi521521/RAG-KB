from __future__ import annotations

import logging

import httpx
import pytest

from app.integrations.dashscope import RerankApiResponse, RerankApiResult
from app.repositories.chunks import ChunkSearchHit
from app.services.reranker import RerankerService


def _hit(chunk_id: int, score: float = 0.1) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=1,
        document_name="guide.md",
        kb_id=2,
        chunk_index=chunk_id,
        content=f"chunk {chunk_id}",
        page_num=None,
        section_title=None,
        score=score,
    )


class FakeRerankerClient:
    def __init__(self, response: RerankApiResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> RerankApiResponse:
        self.calls.append({"query": query, "documents": documents, "top_n": top_n})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class SequenceRerankerClient:
    def __init__(self, responses: list[RerankApiResponse | Exception]) -> None:
        self.responses = responses
        self.calls = 0

    async def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> RerankApiResponse:
        outcome = self.responses[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_rerank_maps_provider_indexes_back_to_original_hits(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeRerankerClient(
        RerankApiResponse(
            results=[
                RerankApiResult(index=2, relevance_score=0.91),
                RerankApiResult(index=0, relevance_score=0.82),
            ],
            total_tokens=123,
        )
    )
    service = RerankerService(client=client, top_n=2)

    with caplog.at_level(logging.INFO, logger="app.services.reranker"):
        result = await service.rerank(question=" original question ", candidates=[_hit(10), _hit(11), _hit(12)])

    assert [hit.chunk_id for hit in result.hits] == [12, 10]
    assert [hit.score for hit in result.hits] == [0.91, 0.82]
    assert result.degraded is False
    assert result.degraded_reason is None
    assert result.input_count == 3
    assert result.output_count == 2
    assert result.total_tokens == 123
    assert client.calls == [
        {
            "query": "original question",
            "documents": ["chunk 10", "chunk 11", "chunk 12"],
            "top_n": 2,
        }
    ]
    assert "[Reranker] 精排完成：候选=3，返回=2" in caplog.text


@pytest.mark.asyncio
async def test_rerank_skips_provider_when_candidate_count_does_not_exceed_top_n(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeRerankerClient(RerankApiResponse(results=[], total_tokens=None))
    service = RerankerService(client=client, top_n=5)
    candidates = [_hit(10), _hit(11)]

    with caplog.at_level(logging.INFO, logger="app.services.reranker"):
        result = await service.rerank(question="question", candidates=candidates)

    assert result.hits == candidates
    assert result.degraded is False
    assert result.degraded_reason == "skipped_not_enough_candidates"
    assert client.calls == []
    assert "[Reranker] 候选数不超过 topN，跳过精排：候选=2，topN=5" in caplog.text


@pytest.mark.asyncio
async def test_rerank_degrades_to_rrf_top_n_when_provider_times_out(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeRerankerClient(httpx.TimeoutException("timeout"))
    service = RerankerService(client=client, top_n=2)
    candidates = [_hit(10, 0.03), _hit(11, 0.02), _hit(12, 0.01)]

    with caplog.at_level(logging.WARNING, logger="app.services.reranker"):
        result = await service.rerank(question="question", candidates=candidates)

    assert result.hits == candidates[:2]
    assert result.degraded is True
    assert result.degraded_reason == "reranker_timeout"
    assert "[Reranker] 精排失败或超时，降级使用 RRF 分数：timeout" in caplog.text


@pytest.mark.asyncio
async def test_rerank_retries_timeout_before_success() -> None:
    client = SequenceRerankerClient(
        [
            httpx.TimeoutException("first timeout"),
            RerankApiResponse(
                results=[RerankApiResult(index=0, relevance_score=0.9)],
                total_tokens=10,
            ),
        ]
    )
    service = RerankerService(client=client, top_n=2, max_retries=1)

    result = await service.rerank(question="question", candidates=[_hit(10), _hit(11), _hit(12)])

    assert result.degraded is False
    assert result.degraded_reason is None
    assert [hit.chunk_id for hit in result.hits] == [10]
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rerank_degrades_after_retry_timeout_is_exhausted() -> None:
    client = SequenceRerankerClient(
        [
            httpx.TimeoutException("first timeout"),
            httpx.TimeoutException("second timeout"),
        ]
    )
    service = RerankerService(client=client, top_n=2, max_retries=1)
    candidates = [_hit(10), _hit(11), _hit(12)]

    result = await service.rerank(question="question", candidates=candidates)

    assert result.degraded is True
    assert result.degraded_reason == "reranker_timeout"
    assert result.hits == candidates[:2]
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rerank_degrades_to_rrf_top_n_when_provider_returns_http_error() -> None:
    request = httpx.Request("POST", "https://example.test/rerank")
    response = httpx.Response(429, request=request)
    client = FakeRerankerClient(httpx.HTTPStatusError("rate limited", request=request, response=response))
    service = RerankerService(client=client, top_n=2)
    candidates = [_hit(10), _hit(11), _hit(12)]

    result = await service.rerank(question="question", candidates=candidates)

    assert result.hits == candidates[:2]
    assert result.degraded is True
    assert result.degraded_reason == "reranker_http_error"


@pytest.mark.asyncio
async def test_rerank_degrades_when_provider_returns_empty_results(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeRerankerClient(RerankApiResponse(results=[], total_tokens=None))
    service = RerankerService(client=client, top_n=2)
    candidates = [_hit(10), _hit(11), _hit(12)]

    with caplog.at_level(logging.WARNING, logger="app.services.reranker"):
        result = await service.rerank(question="question", candidates=candidates)

    assert result.hits == candidates[:2]
    assert result.degraded is True
    assert result.degraded_reason == "reranker_empty_result"
    assert "[Reranker] 精排失败或超时，降级使用 RRF 分数：reranker returned empty results" in caplog.text


@pytest.mark.asyncio
async def test_rerank_degrades_when_provider_index_is_out_of_range() -> None:
    client = FakeRerankerClient(
        RerankApiResponse(results=[RerankApiResult(index=99, relevance_score=0.91)], total_tokens=None)
    )
    service = RerankerService(client=client, top_n=2)
    candidates = [_hit(10), _hit(11), _hit(12)]

    result = await service.rerank(question="question", candidates=candidates)

    assert result.hits == candidates[:2]
    assert result.degraded is True
    assert result.degraded_reason == "reranker_invalid_index"
