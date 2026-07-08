from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.repositories.chunks import ChunkSearchHit


@dataclass(frozen=True)
class RrfSearchHit:
    """RRF 融合后的命中结果。"""

    hit: ChunkSearchHit
    score: float
    retrieval_sources: tuple[str, ...]


def rrf_fuse(
    ranked_results: Mapping[str, Sequence[ChunkSearchHit]],
    *,
    rrf_k: int,
) -> list[RrfSearchHit]:
    """按 RRF 分数融合多路已排序检索结果。

    Args:
        ranked_results: key 为检索通道名，value 为该通道内已排序的命中列表。
        rrf_k: RRF 平滑参数，通常为 60。

    Returns:
        按 RRF 分数降序排列、按 chunk_id 去重后的命中列表。
    """
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive")

    hits_by_id: dict[int, ChunkSearchHit] = {}
    scores_by_id: dict[int, float] = {}
    sources_by_id: dict[int, list[str]] = {}
    first_seen_order: dict[int, int] = {}

    order = 0
    for source_name, hits in ranked_results.items():
        for rank, hit in enumerate(hits, start=1):
            chunk_id = hit.chunk_id
            if chunk_id not in first_seen_order:
                first_seen_order[chunk_id] = order
                order += 1
                sources_by_id[chunk_id] = []

            # 保留首次出现的 chunk 元数据，避免不同通道的原始分数互相污染。
            hits_by_id.setdefault(chunk_id, hit)
            scores_by_id[chunk_id] = scores_by_id.get(chunk_id, 0.0) + (1.0 / (rrf_k + rank))
            if source_name not in sources_by_id[chunk_id]:
                sources_by_id[chunk_id].append(source_name)

    return [
        RrfSearchHit(
            hit=hits_by_id[chunk_id],
            score=scores_by_id[chunk_id],
            retrieval_sources=tuple(sources_by_id[chunk_id]),
        )
        for chunk_id in sorted(
            scores_by_id,
            key=lambda item: (-scores_by_id[item], first_seen_order[item]),
        )
    ]
