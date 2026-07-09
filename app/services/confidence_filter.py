from __future__ import annotations

import logging

from app.repositories.chunks import ChunkSearchHit

logger = logging.getLogger(__name__)


class ConfidenceFilter:
    """低置信度过滤器，仅用于 Reranker 成功后的同尺度分数。"""

    def __init__(self, *, min_score: float) -> None:
        """初始化过滤阈值。"""
        self.min_score = min_score

    def filter(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        """过滤低置信度候选；全部低分时保留最高分 1 条。"""
        if not hits or self.min_score <= 0:
            return hits

        filtered = [hit for hit in hits if hit.score >= self.min_score]
        filtered_count = len(hits) - len(filtered)
        if filtered:
            if filtered_count > 0:
                logger.debug("[ConfidenceFilter] 过滤低置信度 chunk：%s条", filtered_count)
            return filtered

        # 全部低分时保留最高分，避免边界问题过早清空上下文。
        best = max(hits, key=lambda hit: hit.score)
        logger.debug(
            "[ConfidenceFilter] 所有 chunk 低于阈值 %s，保留最高分1条（score=%s）",
            self.min_score,
            best.score,
        )
        logger.debug("[ConfidenceFilter] 过滤低置信度 chunk：%s条", len(hits) - 1)
        return [best]
