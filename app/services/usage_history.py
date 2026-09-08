from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Protocol

from app.repositories.evaluations import EvaluationRunUsageDailySummary

import httpx

_CURRENCY_QUANTUM = Decimal("0.0001")
_SCRAPE_STEP_SECONDS = 15
_HISTORY_STEP_SECONDS = 60
_NEW_SERIES_GAP_SECONDS = 30
_TOKENS_SERIES_QUERY = "rag_token_usage_total"
_COST_SERIES_QUERY = "rag_token_usage_cost_cny_total"


class RunUsageSummaryReader(Protocol):
    """全局面板读取评估 run 总消耗的最小依赖。"""

    async def sum_run_usage_by_day(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> list[EvaluationRunUsageDailySummary]: ...


class UsageHistoryError(RuntimeError):
    """Prometheus 用量历史不可读取时抛出，由路由层转换为 503。"""


@dataclass(frozen=True)
class DailyUsagePoint:
    """单个自然日的全系统 Token 总量与估算成本。"""

    date: str
    tokens: int
    cost: Decimal
    # 评估字段表示完整评估 run：RAG 执行管道 + RAGAS 判定。
    evaluation_tokens: int
    evaluation_cost: Decimal


class UsageHistoryService:
    """从 Prometheus 读取总量，并从 run 用量事实表读取评估 run 总消耗。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        timezone: str,
        run_usage_repository: RunUsageSummaryReader | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("prometheus_query_timeout_seconds must be positive")
        try:
            self.timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown prometheus query timezone: {timezone}") from exc
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.run_usage_repository = run_usage_repository

    async def daily_usage(
        self,
        *,
        days: int,
        now: datetime | None = None,
    ) -> list[DailyUsagePoint]:
        """返回最近 days 个自然日（含当天）的每日用量，历史缺口记 0。"""
        effective_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        dates = self._range_dates(days=days, now=effective_now)
        history_dates = dates[:-1]
        current_time = _epoch(effective_now)
        today_start = _epoch(_midnight(dates[-1], self.timezone))
        evaluation_usage_by_date = await self._load_run_usage_by_date(dates=dates)

        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout_seconds
        ) as client:
            tokens_by_day: dict[date, float] = {}
            cost_by_day: dict[date, float] = {}
            if history_dates:
                history_start = _epoch(_midnight(history_dates[0], self.timezone))
                history_end = _epoch(_next_midnight(history_dates[-1], self.timezone)) - 1
                tokens_by_day = await self._sum_series_increase_by_day(
                    client,
                    _TOKENS_SERIES_QUERY,
                    start=history_start,
                    end=history_end,
                )
                cost_by_day = await self._sum_series_increase_by_day(
                    client,
                    _COST_SERIES_QUERY,
                    start=history_start,
                    end=history_end,
                )

            # 当天尚未到达次日零点，不能再用次日 increase 采样；改用原始 counter 增量。
            today_tokens = await self._sum_series_increase(
                client,
                _TOKENS_SERIES_QUERY,
                start=today_start,
                end=current_time,
            )
            today_cost = await self._sum_series_increase(
                client,
                _COST_SERIES_QUERY,
                start=today_start,
                end=current_time,
            )

        return _build_points(
            dates,
            tokens_by_day,
            cost_by_day,
            evaluation_usage_by_date,
            today_tokens,
            today_cost,
        )

    async def _load_run_usage_by_date(
        self,
        *,
        dates: list[date],
    ) -> dict[date, tuple[int, Decimal]]:
        if self.run_usage_repository is None:
            return {}
        # kb_eval_run_usage.created_at 是部署时区的 naive timestamp，直接用本地零点边界。
        summaries = await self.run_usage_repository.sum_run_usage_by_day(
            start_at=datetime.combine(dates[0], time.min),
            end_at=datetime.combine(dates[-1] + timedelta(days=1), time.min),
        )
        return {
            summary.date: (summary.tokens, summary.cost.quantize(_CURRENCY_QUANTUM))
            for summary in summaries
        }

    def _range_dates(self, *, days: int, now: datetime | None) -> list[date]:
        effective_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        today = effective_now.date()
        return [today - timedelta(offset) for offset in range(days - 1, -1, -1)]

    async def _query_range_series(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        start: int,
        end: int,
        step: int,
    ) -> list[list[tuple[int, float]]]:
        try:
            response = await client.get(
                "/api/v1/query_range",
                params={
                    "query": query,
                    "start": start,
                    "end": end,
                    "step": step,
                },
            )
        except httpx.HTTPError as exc:
            raise UsageHistoryError("Prometheus 不可达") from exc
        if response.status_code != 200:
            raise UsageHistoryError(f"Prometheus 查询失败: status={response.status_code}")
        try:
            payload = response.json()
            results = payload["data"]["result"]
            return [
                [
                    (int(timestamp), float(value))
                    for timestamp, value in sorted(series["values"], key=lambda item: item[0])
                ]
                for series in results
            ]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise UsageHistoryError("Prometheus 响应格式异常") from exc

    async def _sum_series_increase(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        start: int,
        end: int,
    ) -> float:
        series_values = await self._query_range_series(
            client,
            query,
            start=start,
            end=end,
            step=_SCRAPE_STEP_SECONDS,
        )
        return sum(_counter_increase(values, start) for values in series_values)

    async def _sum_series_increase_by_day(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        start: int,
        end: int,
    ) -> dict[date, float]:
        series_values = await self._query_range_series(
            client,
            query,
            start=start,
            end=end,
            step=_HISTORY_STEP_SECONDS,
        )
        totals: dict[date, float] = {}
        for samples in series_values:
            for day, value in _counter_increase_by_day(
                samples, start, self.timezone
            ).items():
                totals[day] = totals.get(day, 0.0) + value
        return totals


def _midnight(day: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone)


def _next_midnight(day: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone)


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _counter_increase(samples: list[tuple[int, float]], range_start: int) -> float:
    """累计单条 counter 序列增量；进程重启导致的回退视为重置后的新值。"""
    if not samples:
        return 0.0

    first_time, first_value = samples[0]
    # 当天中途新建的 Prometheus 序列没有零点样本，若首样本明显晚于起点则计入其首值。
    total = first_value if first_time > range_start + _NEW_SERIES_GAP_SECONDS else 0.0
    previous = first_value
    for _, value in samples[1:]:
        delta = value - previous
        total += value if delta < 0 else delta
        previous = value
    return total


def _counter_increase_by_day(
    samples: list[tuple[int, float]],
    range_start: int,
    timezone: ZoneInfo,
) -> dict[date, float]:
    """按自然日累计单条 counter 序列增量；进程重启导致的回退视为重置后的新值。"""
    if not samples:
        return {}

    totals: dict[date, float] = {}
    first_time, first_value = samples[0]
    # 当天中途新建的序列没有零点样本，首值属于其出现当天。
    if first_time > range_start + _NEW_SERIES_GAP_SECONDS:
        day = datetime.fromtimestamp(first_time, tz=timezone).date()
        totals[day] = totals.get(day, 0.0) + first_value
    previous = first_value
    for timestamp, value in samples[1:]:
        delta = value - previous
        day = datetime.fromtimestamp(timestamp, tz=timezone).date()
        if delta < 0:
            totals[day] = totals.get(day, 0.0) + value
        else:
            totals[day] = totals.get(day, 0.0) + delta
        previous = value
    return totals


def _build_points(
    dates: list[date],
    tokens_by_day: dict[date, float],
    cost_by_day: dict[date, float],
    evaluation_usage_by_date: dict[date, tuple[int, Decimal]],
    today_tokens: float,
    today_cost: float,
) -> list[DailyUsagePoint]:
    """历史日与当天均按原始 counter 序列增量精确累计，确保包含当前时间。"""
    points: list[DailyUsagePoint] = []
    for day in dates:
        if day == dates[-1]:
            tokens = today_tokens
            cost = today_cost
        else:
            tokens = tokens_by_day.get(day, 0.0)
            cost = cost_by_day.get(day, 0.0)
        evaluation_tokens, evaluation_cost = evaluation_usage_by_date.get(day, (0, Decimal("0")))
        points.append(
            DailyUsagePoint(
                date=day.isoformat(),
                tokens=int(tokens),
                cost=Decimal(str(cost)).quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP),
                evaluation_tokens=int(evaluation_tokens),
                evaluation_cost=Decimal(str(evaluation_cost)).quantize(
                    _CURRENCY_QUANTUM, rounding=ROUND_HALF_UP
                ),
            )
        )
    return points
