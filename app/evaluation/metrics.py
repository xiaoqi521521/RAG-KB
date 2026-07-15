from dataclasses import dataclass


TOP_K = 5


@dataclass(frozen=True)
class RetrievalMetrics:
    """单个标准问题的 Hit@5、命中排名和 reciprocal rank。"""

    hit: bool
    rank: int | None
    reciprocal_rank: float


def calculate_retrieval_metrics(
    ranked_chunk_ids: list[int],
    expected_chunk_ids: list[int],
) -> RetrievalMetrics:
    """按固定 Top 5 计算检索指标，多个期望 chunk 取最靠前排名。"""
    expected = set(expected_chunk_ids)
    for rank, chunk_id in enumerate(ranked_chunk_ids[:TOP_K], start=1):
        if chunk_id in expected:
            return RetrievalMetrics(hit=True, rank=rank, reciprocal_rank=1.0 / rank)
    return RetrievalMetrics(hit=False, rank=None, reciprocal_rank=0.0)
