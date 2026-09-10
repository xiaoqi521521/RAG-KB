from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.repositories.evaluations import EvaluationRunUsageDailySummary
from app.services.token_metrics import TokenType


_CURRENCY_QUANTUM = Decimal("0.0001")


class DailyUsageStore(Protocol):
    """读取 Redis 每日非评估 Token 总量并按当前单价派生成本的最小接口。"""

    async def read_daily_usage(
        self,
        *,
        days: int,
        now: datetime,
    ) -> dict[date, dict[TokenType, int]]:
        """返回最近 days 个自然日（含当天）的非评估分类型 Token 数。"""
        ...

    def estimate_cost(self, *, tokens: int, token_type: TokenType) -> Decimal:
        """按当前部署单价估算一笔 Token 消耗的费用。"""
        ...


class RunUsageSummaryReader(Protocol):
    """全局面板读取评估 run 总消耗的最小依赖。"""

    async def sum_run_usage_by_day(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> list[EvaluationRunUsageDailySummary]: ...


class UsageHistoryError(RuntimeError):
    """Redis 用量历史不可读取时抛出，由路由层转换为 503。"""


@dataclass(frozen=True)
class DailyUsagePoint:
    """单个自然日的全系统 Token 总量与估算成本。"""

    date: str
    # Redis 非评估消耗与数据库评估 run 消耗合并后的全口径总量。
    tokens: int
    cost: Decimal
    # 检索问答管道只包含 Redis 非评估消耗，用于与评估口径并列展示。
    retrieval_tokens: int
    retrieval_cost: Decimal
    # 评估字段表示完整评估 run：RAG 执行管道 + RAGAS 判定。
    evaluation_tokens: int
    evaluation_cost: Decimal


class UsageHistoryService:
    """从 Redis 读取每日 Token 总量，并从 run 用量事实表读取评估 run 总消耗。"""

    def __init__(
        self,
        *,
        daily_usage_store: DailyUsageStore,
        timezone: str,
        run_usage_repository: RunUsageSummaryReader | None = None,
    ) -> None:
        try:
            self.timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown usage history timezone: {timezone}") from exc
        self.daily_usage_store = daily_usage_store
        self.run_usage_repository = run_usage_repository

    async def daily_usage(
        self,
        *,
        days: int,
        now: datetime | None = None,
    ) -> list[DailyUsagePoint]:
        """返回最近 days 个自然日（含当天）的每日用量，历史缺口记 0。"""
        if days <= 0:
            raise ValueError("days must be positive")
        effective_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        dates = self._range_dates(days=days, now=effective_now)
        evaluation_usage_by_date = await self._load_run_usage_by_date(dates=dates)

        try:
            token_usage_by_day = await self.daily_usage_store.read_daily_usage(
                days=days,
                now=effective_now,
            )
        except Exception as exc:  # noqa: BLE001
            raise UsageHistoryError("Redis 用量历史不可读") from exc

        points: list[DailyUsagePoint] = []
        for day in dates:
            token_counts = token_usage_by_day.get(day, {})
            tokens = sum(token_counts.values(), 0)
            # 金额不在 Redis 存储，展示时按当前部署单价从 Token 派生。
            cost = sum(
                (
                    self.daily_usage_store.estimate_cost(
                        tokens=count,
                        token_type=token_type,
                    )
                    for token_type, count in token_counts.items()
                ),
                Decimal("0"),
            )
            evaluation_tokens, evaluation_cost = evaluation_usage_by_date.get(
                day,
                (0, Decimal("0")),
            )
            # 评估 run 消耗只在数据库汇总一次，这里并入全口径但保留拆分字段。
            retrieval_cost = cost.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
            tokens += evaluation_tokens
            cost += evaluation_cost
            points.append(
                DailyUsagePoint(
                    date=day.isoformat(),
                    tokens=tokens,
                    cost=cost.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP),
                    retrieval_tokens=tokens - evaluation_tokens,
                    retrieval_cost=retrieval_cost,
                    evaluation_tokens=evaluation_tokens,
                    evaluation_cost=Decimal(evaluation_cost).quantize(
                        _CURRENCY_QUANTUM,
                        rounding=ROUND_HALF_UP,
                    ),
                )
            )
        return points

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
