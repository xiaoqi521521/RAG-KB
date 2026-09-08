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
    _counter_increase_by_day,
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
    responses: dict[tuple[str, int], dict] = {}

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> StubAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None) -> StubResponse:
        assert params is not None
        type(self).requests.append((url, params))
        key = (params["query"], int(params["start"]))
        return StubResponse(200, type(self).responses[key])


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


def test_counter_increase_by_day_splits_days_and_handles_reset() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    midnight = int(datetime(2026, 9, 7, 0, 0, tzinfo=timezone).timestamp())
    # 两天边界样本：首日中途新建序列（首值计入当日），次日发生重置（重置后新值计入当日）。
    samples = [
        (midnight, 1000.0),
        (midnight + 3600, 1100.0),
        (midnight + 86400, 5.0),
        (midnight + 87000, 8.0),
    ]

    totals = _counter_increase_by_day(samples, midnight, timezone)

    assert totals == {
        datetime(2026, 9, 7).date(): 100.0,
        datetime(2026, 9, 8).date(): 8.0,
    }


def test_build_points_uses_per_day_counter_totals() -> None:
    day_one = datetime(2026, 9, 5).date()
    day_two = datetime(2026, 9, 6).date()
    day_three = datetime(2026, 9, 7).date()
    evaluation_usage_by_date = {
        day_one: (80, Decimal("0.008")),
        day_three: (150, Decimal("0.0155")),
    }
    points = _build_points(
        dates=[day_one, day_two, day_three],
        tokens_by_day={day_one: 100.0, day_two: 0.0},
        cost_by_day={day_one: 0.12, day_two: 0.0},
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
        (usage_history._TOKENS_SERIES_QUERY, midnight(7)): _range_payload(
            [[midnight(7), "5000"], [midnight(8) - 60, "56000"]]
        ),
        (usage_history._COST_SERIES_QUERY, midnight(7)): _range_payload(
            [[midnight(7), "0.04"], [midnight(8) - 60, "0.045"]]
        ),
        (usage_history._TOKENS_SERIES_QUERY, midnight(8)): _range_payload(
            [[midnight(8), "56000"], [current_time - 15, "56010"]]
        ),
        (usage_history._COST_SERIES_QUERY, midnight(8)): _range_payload(
            [[midnight(8), "0.045"], [current_time - 15, "0.0505"]]
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
        (51000, Decimal("0.0050")),
        (10, Decimal("0.0055")),
    ]
    assert [(point.evaluation_tokens, point.evaluation_cost) for point in points] == [
        (123, Decimal("0.0123")),
        (170_004, Decimal("0.1955")),
    ]
    range_requests = [
        params for url, params in StubAsyncClient.requests if url.endswith("query_range")
    ]
    assert all(params["step"] == 15 for params in range_requests)
    assert run_usage_repository.calls == [(datetime(2026, 9, 7), datetime(2026, 9, 9))]
