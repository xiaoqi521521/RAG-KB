from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.services.token_cost import TokenCostSummary
from app.services.token_metrics import TokenMetricsUnavailableError
from app.services.usage_history import DailyUsagePoint, UsageHistoryError


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
                intent_tokens=0,
                hyde_tokens=0,
                reranker_tokens=0,
                faithfulness_tokens=0,
                estimated_cost=Decimal("0.0000"),
                embedding_cost=Decimal("0.0000"),
                input_cost=Decimal("0.0000"),
                answer_generation_cost=Decimal("0.0000"),
                intent_cost=Decimal("0.0000"),
                hyde_cost=Decimal("0.0000"),
                reranker_cost=Decimal("0.0000"),
                faithfulness_cost=Decimal("0.0000"),
            )
        return TokenCostSummary(
            embedding_tokens=125_000,
            input_tokens=890_000,
            answer_generation_tokens=210_000,
            intent_tokens=33_000,
            hyde_tokens=0,
            reranker_tokens=0,
            faithfulness_tokens=0,
            estimated_cost=Decimal("1.2195"),
            embedding_cost=Decimal("0.0125"),
            input_cost=Decimal("0.8900"),
            answer_generation_cost=Decimal("0.2100"),
            intent_cost=Decimal("0.1000"),
            hyde_cost=Decimal("0.0050"),
            reranker_cost=Decimal("0.0020"),
            faithfulness_cost=Decimal("0.0000"),
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


class FakeUsageHistoryService:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.requested_days: list[int] = []

    async def daily_usage(self, *, days: int) -> list[DailyUsagePoint]:
        self.requested_days.append(days)
        if self.unavailable:
            raise UsageHistoryError("Prometheus 不可达")
        return [
            DailyUsagePoint(date="2026-09-06", tokens=1_200, cost=Decimal("0.1200")),
            DailyUsagePoint(date="2026-09-07", tokens=3_400, cost=Decimal("0.3400")),
        ]


def _usage_client(role: str, service: FakeUsageHistoryService) -> TestClient:
    from app.api.routes import stats
    from app.core.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(stats.router, prefix="/api/v1/stats")
    app.dependency_overrides[stats.get_current_user] = lambda: CurrentUser(
        user_id=7,
        department_id="engineering",
        role=role,
    )
    app.dependency_overrides[stats.get_usage_history_service] = lambda: service
    return TestClient(app)


def test_daily_usage_returns_points_for_system_admin() -> None:
    service = FakeUsageHistoryService()

    with _usage_client("ADMIN", service) as client:
        response = client.get("/api/v1/stats/usage/daily", params={"days": 2})

    assert response.status_code == 200
    assert response.json() == {
        "code": 200,
        "message": "success",
        "data": [
            {"date": "2026-09-06", "tokens": 1_200, "cost": "0.1200"},
            {"date": "2026-09-07", "tokens": 3_400, "cost": "0.3400"},
        ],
    }
    assert service.requested_days == [2]


def test_daily_usage_rejects_non_admin_users() -> None:
    with _usage_client("MEMBER", FakeUsageHistoryService()) as client:
        response = client.get("/api/v1/stats/usage/daily")

    assert response.status_code == 403
    assert response.json()["message"] == "仅系统管理员可查看全局用量"


def test_daily_usage_returns_503_when_prometheus_unavailable() -> None:
    with _usage_client("ADMIN", FakeUsageHistoryService(unavailable=True)) as client:
        response = client.get("/api/v1/stats/usage/daily")

    assert response.status_code == 503
    assert response.json()["message"] == "用量历史暂不可用"


def test_usage_access_probe_reports_admin_flag() -> None:
    with _usage_client("ADMIN", FakeUsageHistoryService()) as client:
        response = client.get("/api/v1/stats/usage/access")

    assert response.status_code == 200
    assert response.json()["data"] is True


def test_usage_access_probe_reports_false_for_non_admin() -> None:
    with _usage_client("MEMBER", FakeUsageHistoryService()) as client:
        response = client.get("/api/v1/stats/usage/access")

    assert response.status_code == 200
    assert response.json()["data"] is False


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
            "intent_tokens": 33_000,
            "hyde_tokens": 0,
            "reranker_tokens": 0,
            "faithfulness_tokens": 0,
            "estimated_cost": "1.2195",
            "embedding_cost": "0.0125",
            "input_cost": "0.8900",
            "answer_generation_cost": "0.2100",
            "intent_cost": "0.1000",
            "hyde_cost": "0.0050",
            "reranker_cost": "0.0020",
            "faithfulness_cost": "0.0000",
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
        "intent_tokens": 0,
        "hyde_tokens": 0,
        "reranker_tokens": 0,
        "faithfulness_tokens": 0,
        "estimated_cost": "0.0000",
        "embedding_cost": "0.0000",
        "input_cost": "0.0000",
        "answer_generation_cost": "0.0000",
        "intent_cost": "0.0000",
        "hyde_cost": "0.0000",
        "reranker_cost": "0.0000",
        "faithfulness_cost": "0.0000",
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
