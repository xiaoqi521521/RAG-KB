from __future__ import annotations

import logging

import pytest
import tiktoken

from app.repositories.chunks import ChunkSearchHit
from app.services.context_trimmer import ContextTrimmer


class FakeTokenMetrics:
    def __init__(self) -> None:
        self.context_tokens: list[int] = []

    async def record_context_tokens(self, *, tokens: int, pipeline: str = "v4") -> None:
        self.context_tokens.append(tokens)


def _hit(chunk_id: int, content: str, score: float = 0.9) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=7,
        document_name="guide.md",
        kb_id=2,
        chunk_index=chunk_id,
        content=content,
        page_num=3,
        section_title="Policy",
        score=score,
    )


@pytest.mark.asyncio
async def test_trim_keeps_ranked_hits_within_token_budget(caplog: pytest.LogCaptureFixture) -> None:
    metrics = FakeTokenMetrics()
    hits = [_hit(10, "alpha"), _hit(11, "beta"), _hit(12, "gamma")]
    trimmer = ContextTrimmer(max_context_tokens=3, token_metrics=metrics)

    with caplog.at_level(logging.INFO, logger="app.services.context_trimmer"):
        trimmed = await trimmer.trim(hits)

    assert trimmed == hits
    assert metrics.context_tokens == [3]
    assert "input_count=3" in caplog.text
    assert "selected_count=3" in caplog.text
    assert "used_tokens=3/3" in caplog.text


@pytest.mark.asyncio
async def test_trim_stops_when_next_ranked_hit_exceeds_remaining_budget() -> None:
    metrics = FakeTokenMetrics()
    hits = [_hit(10, "alpha"), _hit(11, "beta"), _hit(12, "gamma")]
    trimmer = ContextTrimmer(max_context_tokens=2, token_metrics=metrics)

    trimmed = await trimmer.trim(hits)

    assert trimmed == hits[:2]
    assert metrics.context_tokens == [2]


@pytest.mark.asyncio
async def test_trim_truncates_first_hit_and_preserves_source_metadata() -> None:
    metrics = FakeTokenMetrics()
    original = _hit(10, "alpha beta gamma delta", score=0.97)
    trimmer = ContextTrimmer(max_context_tokens=2, token_metrics=metrics)

    trimmed = await trimmer.trim([original])

    assert len(trimmed) == 1
    assert trimmed[0] is not original
    assert trimmed[0].content != original.content
    assert len(tiktoken.get_encoding("cl100k_base").encode(trimmed[0].content)) <= 2
    assert trimmed[0].chunk_id == original.chunk_id
    assert trimmed[0].doc_id == original.doc_id
    assert trimmed[0].document_name == original.document_name
    assert trimmed[0].kb_id == original.kb_id
    assert trimmed[0].chunk_index == original.chunk_index
    assert trimmed[0].page_num == original.page_num
    assert trimmed[0].section_title == original.section_title
    assert trimmed[0].score == original.score
    assert metrics.context_tokens == [
        len(tiktoken.get_encoding("cl100k_base").encode(trimmed[0].content))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_context_tokens", "hits"),
    [
        (10, []),
        (0, [_hit(10, "alpha")]),
        (-1, [_hit(10, "alpha")]),
    ],
)
async def test_trim_returns_empty_and_records_zero_without_available_context(
    max_context_tokens: int,
    hits: list[ChunkSearchHit],
) -> None:
    metrics = FakeTokenMetrics()
    trimmer = ContextTrimmer(
        max_context_tokens=max_context_tokens,
        token_metrics=metrics,
    )

    assert await trimmer.trim(hits) == []
    assert metrics.context_tokens == [0]
