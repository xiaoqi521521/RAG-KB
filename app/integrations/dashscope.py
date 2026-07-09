from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class RerankApiResult:
    """DashScope Reranker 返回的单条排序结果。"""

    index: int
    relevance_score: float


@dataclass(frozen=True)
class RerankApiResponse:
    """DashScope Reranker 结构化响应。"""

    results: list[RerankApiResult]
    total_tokens: int | None


class DashScopeRerankerClient:
    """DashScope Reranker HTTP 客户端，只负责请求 provider 和解析响应。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化客户端。

        Args:
            settings: 应用配置；测试可显式传入，生产默认读取全局配置。
        """
        self.settings = settings or get_settings()
        self.timeout = self.settings.reranker_timeout_ms / 1000

    async def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> RerankApiResponse:
        """调用 DashScope Reranker，返回带原始下标的精排结果。"""
        normalized_query = query.strip()
        if not documents or not normalized_query or top_n <= 0:
            return RerankApiResponse(results=[], total_tokens=None)

        effective_top_n = min(top_n, len(documents))
        payload = {
            "model": self.settings.reranker_model,
            "query": normalized_query,
            "documents": documents,
            "top_n": effective_top_n,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.settings.reranker_endpoint,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        results_data = data["results"]
        if not isinstance(results_data, list):
            raise ValueError("DashScope reranker response output.results must be a list")

        results: list[RerankApiResult] = []
        for item in results_data:
            results.append(
                RerankApiResult(
                    index=int(item["index"]),
                    relevance_score=float(item["relevance_score"]),
                )
            )

        usage = data.get("usage")
        total_tokens = None
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            total_tokens = int(usage["total_tokens"])

        return RerankApiResponse(results=results, total_tokens=total_tokens)
