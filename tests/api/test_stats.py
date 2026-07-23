from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.services.token_cost import TokenCostSummary
from app.services.token_metrics import TokenMetricsUnavailableError


class FakeTokenCostService:
    def __init__(self, *, unavailable: bool = False, zero: bool = False) -> None:
        self.unavailable = unavailable
        self.zero = zero
        self.user_ids: list[int] = []

    async def get_user_cost(self, *, user_id: int) -> TokenCostSummary:
        self.user_ids.append(user_id)
        if self.unavailable:
            raise TokenMetricsUnavailableError("Token 统计暂不可用")
        if self.zero:
            return TokenCostSummary(
                embedding_tokens=0,
                input_tokens=0,
                answer_generation_tokens=0,
                hyde_tokens=0,
                reranker_tokens=0,
                faithfulness_tokens=0,
                estimated_cost=Decimal("0.0000"),
            )
        return TokenCostSummary(
            embedding_tokens=125_000,
            input_tokens=890_000,
            answer_generation_tokens=210_000,
            hyde_tokens=0,
            reranker_tokens=0,
            faithfulness_tokens=0,
            estimated_cost=Decimal("1.2195"),
        )


def _client(cost_service: FakeTokenCostService) -> TestClient:
    from app.api.routes import stats
    from app.core.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(stats.router, prefix="/api/v1/stats")
    app.dependency_overrides[stats.get_current_user] = lambda: CurrentUser(
        user_id=7,
        department_id="engineering",
        role="MEMBER",
    )
    app.dependency_overrides[stats.get_token_cost_service] = lambda: cost_service
    return TestClient(app)


def test_token_stats_returns_current_user_cost_summary() -> None:
    cost_service = FakeTokenCostService()

    with _client(cost_service) as client:
        response = client.get("/api/v1/stats/tokens")

    assert response.status_code == 200
    assert response.json() == {
        "code": 200,
        "message": "success",
        "data": {
            "embedding_tokens": 125_000,
            "input_tokens": 890_000,
            "answer_generation_tokens": 210_000,
            "hyde_tokens": 0,
            "reranker_tokens": 0,
            "faithfulness_tokens": 0,
            "estimated_cost": "1.2195",
            "currency": "CNY",
        },
    }
    assert cost_service.user_ids == [7]


def test_token_stats_returns_503_when_redis_stats_are_unavailable() -> None:
    cost_service = FakeTokenCostService(unavailable=True)

    with _client(cost_service) as client:
        response = client.get("/api/v1/stats/tokens")

    assert response.status_code == 503
    assert response.json() == {
        "code": 503,
        "message": "Token 统计暂不可用",
        "data": None,
    }


def test_token_stats_returns_zero_for_user_without_usage() -> None:
    cost_service = FakeTokenCostService(zero=True)

    with _client(cost_service) as client:
        response = client.get("/api/v1/stats/tokens")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "embedding_tokens": 0,
        "input_tokens": 0,
        "answer_generation_tokens": 0,
        "hyde_tokens": 0,
        "reranker_tokens": 0,
        "faithfulness_tokens": 0,
        "estimated_cost": "0.0000",
        "currency": "CNY",
    }


def test_token_stats_requires_authentication() -> None:
    from app.api.routes import stats
    from app.core.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(stats.router, prefix="/api/v1/stats")
    app.dependency_overrides[stats.get_token_cost_service] = lambda: FakeTokenCostService()

    with TestClient(app) as client:
        response = client.get("/api/v1/stats/tokens")

    assert response.status_code == 401
    assert response.json()["code"] == 401
