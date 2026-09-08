from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services import usage_history
from app.services.usage_history import (
    UsageHistoryError,
    UsageHistoryService,
    _build_points,
    _counter_increase,
)
from app.repositories.evaluations import EvaluationRunUsageDailySummary


class StubResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class StubAsyncClient:
    requests: list[tuple[str, dict]] = []
    responses: dict[str, dict] = {}

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> StubAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None) -> StubResponse:
        assert params is not None
        type(self).requests.append((url, params))
        return StubResponse(200, type(self).responses[params["query"]])


def _range_payload(values: list[tuple[int, str]]) -> dict:
    return {"status": "success", "data": {"result": [{"metric": {}, "values": values}]}}


def _empty_payload() -> dict:
    return {"status": "success", "data": {"result": []}}


class StubRunUsageRepository:
    def __init__(self, summaries: list[EvaluationRunUsageDailySummary]) -> None:
        self.summaries = summaries
        self.calls: list[tuple[datetime, datetime]] = []

    async def sum_run_usage_by_day(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> list[EvaluationRunUsageDailySummary]:
        self.calls.append((start_at, end_at))
        return self.summaries


@pytest.mark.asyncio
async def test_daily_usage_maps_http_failures_to_history_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = UsageHistoryService(
        base_url="http://prometheus:9090",
        timeout_seconds=5.0,
        timezone="Asia/Shanghai",
    )

    class BrokenClient(StubAsyncClient):
        async def get(self, url: str, params: dict | None = None) -> StubResponse:
            raise httpx.ConnectError("down")

    monkeypatch.setattr(usage_history.httpx, "AsyncClient", lambda **kwargs: BrokenClient())

    with pytest.raises(UsageHistoryError):
        await service.daily_usage(days=1)


def test_counter_increase_handles_new_series_and_reset() -> None:
    values = [(100, 1000.0), (115, 1100.0), (130, 5.0), (145, 8.0)]

    assert _counter_increase(values, 100) == 108.0
    # 当天中途出现的新序列没有零点样本，首值属于当天。
    assert _counter_increase([(200, 42.0)], 100) == 42.0
    assert _counter_increase([], 100) == 0.0


def test_build_points_uses_history_increase_and_today_counter_total() -> None:
    evaluation_usage_by_date = {
        datetime(2026, 9, 5).date(): (80, Decimal("0.008")),
        datetime(2026, 9, 7).date(): (150, Decimal("0.0155")),
    }
    points = _build_points(
        dates=[
            datetime(2026, 9, 5).date(),
            datetime(2026, 9, 6).date(),
            datetime(2026, 9, 7).date(),
        ],
        history_sample_times=[200, 300],
        tokens_by_time={200: 100.0, 300: 0.0},
        cost_by_time={200: 0.12, 300: 0.0},
        today_tokens=250.9,
        today_cost=0.0205,
        evaluation_usage_by_date=evaluation_usage_by_date,
    )

    assert [point.date for point in points] == ["2026-09-05", "2026-09-06", "2026-09-07"]
    assert [point.tokens for point in points] == [100, 0, 250]
    assert [point.cost for point in points] == [
        Decimal("0.1200"),
        Decimal("0.0000"),
        Decimal("0.0205"),
    ]
    assert [point.evaluation_tokens for point in points] == [80, 0, 150]
    assert [point.evaluation_cost for point in points] == [
        Decimal("0.0080"),
        Decimal("0.0000"),
        Decimal("0.0155"),
    ]


@pytest.mark.asyncio
async def test_daily_usage_combines_history_and_today_without_evaluation_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    run_usage_repository = StubRunUsageRepository(
        [
            EvaluationRunUsageDailySummary(
                date=datetime(2026, 9, 7).date(),
                tokens=123,
                cost=Decimal("0.0123"),
            ),
            EvaluationRunUsageDailySummary(
                date=datetime(2026, 9, 8).date(),
                tokens=170_004,
                cost=Decimal("0.195498"),
            ),
        ]
    )
    service = UsageHistoryService(
        base_url="http://prometheus:9090",
        timeout_seconds=5.0,
        timezone="Asia/Shanghai",
        run_usage_repository=run_usage_repository,
    )

    def midnight(day: int) -> int:
        return int(datetime(2026, 9, day, 0, 0, tzinfo=timezone).timestamp())

    current_time = int(datetime(2026, 9, 8, 13, 30, tzinfo=timezone).timestamp())
    StubAsyncClient.requests = []
    StubAsyncClient.responses = {
        usage_history._TOKENS_QUERY: _range_payload([[midnight(8), "68887"]]),
        usage_history._COST_QUERY: _range_payload([[midnight(8), "0.046"]]),
        usage_history._TOKENS_SERIES_QUERY: _range_payload(
            [[midnight(8), "1000"], [current_time - 15, "1010"]]
        ),
        usage_history._COST_SERIES_QUERY: _range_payload(
            [[midnight(8), "0.05"], [current_time - 15, "0.0505"]]
        ),
    }
    monkeypatch.setattr(
        usage_history.httpx,
        "AsyncClient",
        lambda **kwargs: StubAsyncClient(),
    )

    points = await service.daily_usage(
        days=2,
        now=datetime(2026, 9, 8, 13, 30, tzinfo=timezone),
    )

    assert [(point.tokens, point.cost) for point in points] == [
        (68887, Decimal("0.0460")),
        (10, Decimal("0.0005")),
    ]
    assert [(point.evaluation_tokens, point.evaluation_cost) for point in points] == [
        (123, Decimal("0.0123")),
        (170_004, Decimal("0.1955")),
    ]
    range_requests = [
        params for url, params in StubAsyncClient.requests if url.endswith("query_range")
    ]
    assert all(params["step"] == 86400 for params in range_requests[:2])
    assert all(params["step"] == 15 for params in range_requests[2:])
    assert run_usage_repository.calls == [(datetime(2026, 9, 7), datetime(2026, 9, 9))]
