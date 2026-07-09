from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.integrations.dashscope import DashScopeRerankerClient


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
        dashscope_api_key="test-key",
        reranker_model="qwen3-rerank",
        reranker_timeout_ms=800,
    )


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeAsyncClient:
    calls: list[dict[str, object]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
    ) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout})
        return FakeResponse(
            {
                "object": "list",
                "results": [
                    {"index": 2, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.82},
                ],
                "model": "qwen3-rerank",
                "id": "request-id",
                "usage": {"total_tokens": 123},
            }
        )


@pytest.mark.asyncio
async def test_dashscope_reranker_client_sends_qwen3_payload_and_parses_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations import dashscope

    FakeAsyncClient.calls = []
    monkeypatch.setattr(dashscope.httpx, "AsyncClient", FakeAsyncClient)
    client = DashScopeRerankerClient(settings=_settings())

    response = await client.rerank(query=" question ", documents=["a", "b", "c"], top_n=5)

    assert [(item.index, item.relevance_score) for item in response.results] == [(2, 0.91), (0, 0.82)]
    assert response.total_tokens == 123
    assert FakeAsyncClient.calls == [
        {
            "url": "https://example.test/rerank",
            "json": {
                "model": "qwen3-rerank",
                "query": "question",
                "documents": ["a", "b", "c"],
                "top_n": 3,
            },
            "headers": {
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
            },
            "timeout": 0.8,
        }
    ]


@pytest.mark.asyncio
async def test_dashscope_reranker_client_returns_empty_response_without_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations import dashscope

    monkeypatch.setattr(dashscope.httpx, "AsyncClient", SimpleNamespace)
    client = DashScopeRerankerClient(settings=_settings())

    response = await client.rerank(query="question", documents=[], top_n=5)

    assert response.results == []
    assert response.total_tokens is None
