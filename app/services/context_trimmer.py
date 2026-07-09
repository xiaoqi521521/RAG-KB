from __future__ import annotations

from app.repositories.chunks import ChunkSearchHit


class ContextTrimmer:
    """上下文裁剪占位服务，第 16 章替换为 token 预算裁剪实现。"""

    def trim(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        """本阶段不做裁剪，原样返回候选列表。"""
        return hits
