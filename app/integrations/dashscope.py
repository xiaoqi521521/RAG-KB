import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import get_settings


class DashScopeRerankerClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.timeout = self.settings.reranker_timeout_ms / 1000

    @retry(wait=wait_exponential(multiplier=0.2, max=1), stop=stop_after_attempt(2))
    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        payload = {
            "model": self.settings.reranker_model,
            "input": {"query": query, "documents": documents},
            "parameters": {"top_n": self.settings.reranker_top_n},
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
        return [item["relevance_score"] for item in data["output"]["results"]]
