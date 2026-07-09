from __future__ import annotations

from app.repositories.chunks import ChunkSearchHit
from app.services.context_trimmer import ContextTrimmer


def _hit(chunk_id: int, score: float) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=1,
        document_name="guide.md",
        kb_id=2,
        chunk_index=chunk_id,
        content=f"chunk {chunk_id}",
        page_num=3,
        section_title="Policy",
        score=score,
    )


def test_trim_returns_hits_unchanged_as_placeholder() -> None:
    hits = [_hit(10, 0.91), _hit(11, 0.82)]

    trimmed = ContextTrimmer().trim(hits)

    assert trimmed == hits


def test_trim_returns_empty_list_when_no_hits() -> None:
    assert ContextTrimmer().trim([]) == []
