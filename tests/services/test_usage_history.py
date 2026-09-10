from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.repositories.evaluations import EvaluationRunUsageDailySummary
from app.services.usage_history import DailyUsagePoint, UsageHistoryError, UsageHistoryService


timezone = ZoneInfo("Asia/Shanghai")
_PRICES = {
    "embedding": Decimal("0.0005"),
    "input": Decimal("0.001"),
    "answer_generation": Decimal("0.002"),
    "intent": Decimal("0.002"),
    "hyde": Decimal("0.002"),
    "reranker": Decimal("0.0005"),
    "faithfulness_check": Decimal("0.002"),
    "evaluation": Decimal("0.002"),
}


class FakeDailyUsageStore:
    def __init__(self, usage_by_day: dict[date, dict[str, int]] | None = None) -> None:
        self.usage_by_day = usage_by_day or {}
        self.fail = False
        self.calls: list[tuple[int, datetime]] = []

    async def read_daily_usage(
        self,
        *,
        days: int,
        now: datetime,
    ) -> dict[date, dict[str, int]]:
        self.calls.append((days, now))
        if self.fail:
            raise RuntimeError("redis unavailable")
        return self.usage_by_day

    def estimate_cost(self, *, tokens: int, token_type: str) -> Decimal:
        if tokens <= 0:
            return Decimal("0")
        return Decimal(tokens) / Decimal("1000") * _PRICES[token_type]


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
async def test_daily_usage_derives_cost_and_merges_evaluation_usage() -> None:
    day_one = date(2026, 9, 6)
    day_three = date(2026, 9, 8)
    now = datetime(2026, 9, 8, 13, 30, tzinfo=timezone)
    daily_store = FakeDailyUsageStore(
        {
            day_one: {"input": 1_000, "embedding": 2_000, "answer_generation": 500},
            day_three: {"reranker": 4_000},
        }
    )
    run_usage_repository = StubRunUsageRepository(
        [
            EvaluationRunUsageDailySummary(
                date=day_one,
                tokens=123,
                cost=Decimal("0.0123"),
            ),
            EvaluationRunUsageDailySummary(
                date=day_three,
                tokens=170_004,
                cost=Decimal("0.195498"),
            ),
        ]
    )
    service = UsageHistoryService(
        daily_usage_store=daily_store,
        timezone="Asia/Shanghai",
        run_usage_repository=run_usage_repository,
    )

    points = await service.daily_usage(days=3, now=now)

    assert [point.date for point in points] == ["2026-09-06", "2026-09-07", "2026-09-08"]
    assert [point.tokens for point in points] == [3_623, 0, 174_004]
    assert [point.cost for point in points] == [
        Decimal("0.0153"),
        Decimal("0.0000"),
        Decimal("0.1975"),
    ]
    assert [(point.evaluation_tokens, point.evaluation_cost) for point in points] == [
        (123, Decimal("0.0123")),
        (0, Decimal("0.0000")),
        (170_004, Decimal("0.1955")),
    ]
    assert daily_store.calls == [(3, now)]
    assert run_usage_repository.calls == [(datetime(2026, 9, 6), datetime(2026, 9, 9))]


@pytest.mark.asyncio
async def test_daily_usage_works_without_evaluation_repository() -> None:
    today = date(2026, 9, 8)
    now = datetime(2026, 9, 8, 13, 30, tzinfo=timezone)
    daily_store = FakeDailyUsageStore({today: {"input": 20_000, "hyde": 5_000}})
    service = UsageHistoryService(
        daily_usage_store=daily_store,
        timezone="Asia/Shanghai",
    )

    points = await service.daily_usage(days=1, now=now)

    assert points == [
        DailyUsagePoint(
            date="2026-09-08",
            tokens=25_000,
            cost=Decimal("0.0300"),
            evaluation_tokens=0,
            evaluation_cost=Decimal("0.0000"),
        )
    ]


@pytest.mark.asyncio
async def test_daily_usage_maps_redis_failures_to_history_error() -> None:
    daily_store = FakeDailyUsageStore()
    daily_store.fail = True
    run_usage_repository = StubRunUsageRepository([])
    service = UsageHistoryService(
        daily_usage_store=daily_store,
        timezone="Asia/Shanghai",
        run_usage_repository=run_usage_repository,
    )

    with pytest.raises(UsageHistoryError):
        await service.daily_usage(
            days=2,
            now=datetime(2026, 9, 8, 13, 30, tzinfo=timezone),
        )
