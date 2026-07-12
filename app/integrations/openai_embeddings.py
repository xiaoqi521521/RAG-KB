from __future__ import annotations

from typing import Any


class OpenAICompatibleEmbeddings:
    """调用 OpenAI-compatible Embedding API，并保留 provider usage。"""

    def __init__(self, *, embeddings_api: Any, model: str) -> None:
        self.embeddings_api = embeddings_api
        self.model = model

    async def aembed_documents_with_usage(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int | None]:
        """生成文本向量，同时返回 provider 报告的总 token 数。"""
        response = await self.embeddings_api.create(input=texts, model=self.model)

        # provider 应按输入顺序返回，但仍按 index 排序，避免兼容接口乱序导致向量错位。
        items = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in items]
        usage = getattr(response, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None)
        return vectors, int(total_tokens) if total_tokens is not None else None
