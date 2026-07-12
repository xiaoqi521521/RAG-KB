from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol

import tiktoken

from app.repositories.chunks import ChunkSearchHit

logger = logging.getLogger(__name__)


class ContextTokenRecorder(Protocol):
    """记录裁剪后 RAG 正文 token 的最小依赖接口。"""

    async def record_context_tokens(self, *, tokens: int, pipeline: str = "v4") -> None: ...


class ContextTrimmer:
    """按 token 预算裁剪已排序候选，并记录实际保留的 RAG 正文消耗。"""

    def __init__(
        self,
        *,
        max_context_tokens: int,
        token_metrics: ContextTokenRecorder,
        encoding_name: str = "cl100k_base",
    ) -> None:
        self.max_context_tokens = max_context_tokens
        self.token_metrics = token_metrics
        self._encoding = tiktoken.get_encoding(encoding_name)

    async def trim(self, hits: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
        """按预算贪心保留候选，返回与输入相同类型的已裁剪列表。"""
        selected: list[ChunkSearchHit] = []
        used_tokens = 0
        truncated = False

        if self.max_context_tokens > 0:
            for hit in hits:
                chunk_tokens = self.count_tokens(hit.content)
                if used_tokens + chunk_tokens <= self.max_context_tokens:
                    selected.append(hit)
                    used_tokens += chunk_tokens
                    continue

                if not selected:
                    # 首个高相关 chunk 超预算时保留可用前缀，避免有证据却完全无上下文。
                    truncated_content = self.truncate_to_tokens(
                        hit.content,
                        self.max_context_tokens - used_tokens,
                    )
                    if truncated_content:
                        selected.append(replace(hit, content=truncated_content))
                        used_tokens += self.count_tokens(truncated_content)
                        truncated = True
                break

        logger.info(
            ("Context trimmed: input_count=%s selected_count=%s used_tokens=%s/%s truncated=%s"),
            len(hits),
            len(selected),
            used_tokens,
            self.max_context_tokens,
            truncated,
        )
        await self.token_metrics.record_context_tokens(tokens=used_tokens)
        return selected

    def count_tokens(self, text: str | None) -> int:
        """统计正文 token 数，空文本返回 0。"""
        if not text or not text.strip():
            return 0
        return len(self._encoding.encode(text))

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """将正文截断到指定 token 上限，并返回可进入上下文的文本。"""
        if max_tokens <= 0:
            return ""
        tokens = self._encoding.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return self._encoding.decode(tokens[:max_tokens]).strip()
