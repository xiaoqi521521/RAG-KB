from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

_CURRENCY_QUANTUM = Decimal("0.0001")
_DAY_SECONDS = 86400
_TOKENS_QUERY = "sum(increase(rag_token_usage_total[1d]))"
_COST_QUERY = "sum(increase(rag_token_usage_cost_cny_total[1d]))"


class UsageHistoryError(RuntimeError):
    """Prometheus 用量历史不可读取时抛出，由路由层转换为 503。"""


@dataclass(frozen=True)
class DailyUsagePoint:
    """单个自然日的全系统 Token 总量与估算成本。"""

    date: str
    tokens: int
    cost: Decimal


class UsageHistoryService:
    """从 Prometheus 读取按部署时区自然日汇总的 Token 与估算成本。"""

    def __init__(self, *, base_url: str, timeout_seconds: float, timezone: str) -> None:
        if timeout_seconds <= 0:
            raise ValueError("prometheus_query_timeout_seconds must be positive")
        try:
            self.timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown prometheus query timezone: {timezone}") from exc
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def daily_usage(
        self,
        *,
        days: int,
        now: datetime | None = None,
    ) -> list[DailyUsagePoint]:
        """返回最近 days 个自然日（含当天）的每日用量，历史缺口记 0。"""
        dates = self._range_dates(days=days, now=now)
        # increase 的窗口是 (t-1d, t]，把采样点对齐到次日零点，窗口恰好覆盖完整自然日。
        sample_times = [_epoch(_next_midnight(day, self.timezone)) for day in dates]
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            tokens_by_time = await self._query_range(
                client,
                _TOKENS_QUERY,
                start=sample_times[0],
                end=sample_times[-1],
            )
            cost_by_time = await self._query_range(
                client,
                _COST_QUERY,
                start=sample_times[0],
                end=sample_times[-1],
            )
        return _build_points(dates, sample_times, tokens_by_time, cost_by_time)

    def _range_dates(self, *, days: int, now: datetime | None) -> list[date]:
        effective_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        today = effective_now.date()
        return [today - timedelta(offset) for offset in range(days - 1, -1, -1)]

    async def _query_range(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        start: int,
        end: int,
    ) -> dict[int, float]:
        try:
            response = await client.get(
                "/api/v1/query_range",
                params={"query": query, "start": start, "end": end, "step": _DAY_SECONDS},
            )
        except httpx.HTTPError as exc:
            raise UsageHistoryError("Prometheus 不可达") from exc
        if response.status_code != 200:
            raise UsageHistoryError(f"Prometheus 查询失败: status={response.status_code}")
        try:
            payload = response.json()
            series = payload["data"]["result"][0]
            raw_values = series.get("values", [])
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise UsageHistoryError("Prometheus 响应格式异常") from exc
        samples: dict[int, float] = {}
        for timestamp, raw_value in raw_values:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                samples[int(timestamp)] = value
        return samples


def _next_midnight(day: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone)


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _build_points(
    dates: list[date],
    sample_times: list[int],
    tokens_by_time: dict[int, float],
    cost_by_time: dict[int, float],
) -> list[DailyUsagePoint]:
    """把两条 range 序列对齐到自然日，缺失日期记 0，保证返回连续天数。"""
    points: list[DailyUsagePoint] = []
    for day, sample_time in zip(dates, sample_times, strict=True):
        cost = Decimal(str(cost_by_time.get(sample_time, 0.0)))
        points.append(
            DailyUsagePoint(
                date=day.isoformat(),
                tokens=int(tokens_by_time.get(sample_time, 0)),
                cost=cost.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP),
            )
        )
    return points
