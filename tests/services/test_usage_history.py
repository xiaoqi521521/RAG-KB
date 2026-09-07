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
)


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


def _prometheus_payload(query: str, values: list[tuple[int, str]]) -> dict:
    return {"status": "success", "data": {"result": [{"metric": {}, "values": values}]}}


@pytest.mark.asyncio
async def test_daily_usage_aligns_windows_to_local_midnights(monkeypatch: pytest.MonkeyPatch) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    service = UsageHistoryService(
        base_url="http://prometheus:9090/",
        timeout_seconds=5.0,
        timezone="Asia/Shanghai",
    )
    tokens_query = "sum(increase(rag_token_usage_total[1d]))"
    cost_query = "sum(increase(rag_token_usage_cost_cny_total[1d]))"
    day = lambda offset: int(  # noqa: E731
        datetime(2026, 9, 5 + offset, 0, 0, tzinfo=timezone).timestamp()
    ) + 86400
    StubAsyncClient.requests = []
    StubAsyncClient.responses = {
        tokens_query: _prometheus_payload(
            tokens_query,
            [[day(0), "100"], [day(2), "250.9"]],
        ),
        cost_query: _prometheus_payload(
            cost_query,
            [[day(0), "0.12"], [day(1), "0.02"]],
        ),
    }

    def fake_async_client(**kwargs: object) -> StubAsyncClient:
        return StubAsyncClient(**kwargs)

    monkeypatch.setattr(usage_history.httpx, "AsyncClient", fake_async_client)

    points = await service.daily_usage(
        days=3,
        now=datetime(2026, 9, 7, 15, 30, tzinfo=timezone),
    )

    assert [point.date for point in points] == ["2026-09-05", "2026-09-06", "2026-09-07"]
    assert [point.tokens for point in points] == [100, 0, 250]
    assert [point.cost for point in points] == [
        Decimal("0.1200"),
        Decimal("0.0200"),
        Decimal("0.0000"),
    ]
    assert [url for url, _ in StubAsyncClient.requests] == [
        "/api/v1/query_range",
        "/api/v1/query_range",
    ]
    assert all(params["step"] == 86400 for _, params in StubAsyncClient.requests)
    assert all(params["start"] == day(0) for _, params in StubAsyncClient.requests)
    assert all(params["end"] == day(2) for _, params in StubAsyncClient.requests)


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


def test_build_points_fills_missing_days_with_zero() -> None:
    points = _build_points(
        dates=[datetime(2026, 9, 6).date(), datetime(2026, 9, 7).date()],
        sample_times=[100, 200],
        tokens_by_time={200: 7.0},
        cost_by_time={100: 0.5},
    )

    assert [(point.date, point.tokens, point.cost) for point in points] == [
        ("2026-09-06", 0, Decimal("0.5000")),
        ("2026-09-07", 7, Decimal("0.0000")),
    ]
