from __future__ import annotations

import logging

from app.repositories.chunks import ChunkSearchHit
from app.services.confidence_filter import ConfidenceFilter


def _hit(chunk_id: int, score: float) -> ChunkSearchHit:
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


def test_filter_keeps_hits_at_or_above_threshold_in_original_order(caplog) -> None:
    hits = [_hit(10, 0.92), _hit(11, 0.41), _hit(12, 0.5)]
    with caplog.at_level(logging.DEBUG, logger="app.services.confidence_filter"):
        filtered = ConfidenceFilter(min_score=0.5).filter(hits)

    assert [hit.chunk_id for hit in filtered] == [10, 12]
    assert "[ConfidenceFilter] 过滤低置信度 chunk：1条" in caplog.text


def test_filter_keeps_highest_score_when_all_hits_are_below_threshold(caplog) -> None:
    hits = [_hit(10, 0.21), _hit(11, 0.19), _hit(12, 0.08)]
    with caplog.at_level(logging.DEBUG, logger="app.services.confidence_filter"):
        filtered = ConfidenceFilter(min_score=0.5).filter(hits)

    assert [hit.chunk_id for hit in filtered] == [10]
    assert "[ConfidenceFilter] 所有 chunk 低于阈值 0.5，保留最高分1条（score=0.21）" in caplog.text
    assert "[ConfidenceFilter] 过滤低置信度 chunk：2条" in caplog.text


def test_filter_returns_empty_list_when_no_hits() -> None:
    assert ConfidenceFilter(min_score=0.5).filter([]) == []


def test_filter_is_disabled_when_threshold_is_not_positive() -> None:
    hits = [_hit(10, 0.01), _hit(11, -0.2)]
    filtered = ConfidenceFilter(min_score=0).filter(hits)

    assert filtered == hits
