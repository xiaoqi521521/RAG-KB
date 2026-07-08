from __future__ import annotations

import pytest

from app.repositories.chunks import ChunkSearchHit
from app.services.rrf import rrf_fuse


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


def test_rrf_scores_single_ranked_route() -> None:
    fused = rrf_fuse({"vector": [_hit(10)]}, rrf_k=60)

    assert len(fused) == 1
    assert fused[0].hit.chunk_id == 10
    assert fused[0].score == 1 / 61
    assert fused[0].retrieval_sources == ("vector",)


def test_rrf_deduplicates_and_accumulates_scores_across_routes() -> None:
    fused = rrf_fuse(
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
    fused = rrf_fuse(
        {
            "vector": [_hit(10), _hit(11)],
            "fulltext": [_hit(12), _hit(13)],
        },
        rrf_k=60,
    )

    assert [item.hit.chunk_id for item in fused] == [10, 12, 11, 13]


def test_rrf_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError, match="rrf_k must be positive"):
        rrf_fuse({"vector": [_hit(10)]}, rrf_k=0)
