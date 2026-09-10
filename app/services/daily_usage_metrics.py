from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Meter, ObservableGauge, Observation


class DailyUsageMetrics:
    """缓存全局用量服务计算的当日 Token 与估算成本。"""

    def __init__(self, *, meter: Meter | None = None) -> None:
        effective_meter = meter or metrics.get_meter("rag-kb.daily-usage")
        self._tokens = 0
        self._cost = Decimal("0")
        self._tokens_gauge: ObservableGauge = effective_meter.create_observable_gauge(
            "rag_daily_usage_tokens",
            description="Daily usage tokens from Redis and evaluation run database",
            callbacks=[self._observe_tokens],
        )
        self._cost_gauge: ObservableGauge = effective_meter.create_observable_gauge(
            "rag_daily_usage_cost_cny",
            description="Daily usage estimated cost from Redis and evaluation run database",
            callbacks=[self._observe_cost],
        )

    def update(self, *, tokens: int, cost_cny: Decimal) -> None:
        """用全局用量服务的当日聚合结果刷新 Gauge 缓存。"""
        self._tokens = tokens
        self._cost = cost_cny

    def _observe_tokens(self, options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(self._tokens, {"scope": "global"})]

    def _observe_cost(self, options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(float(self._cost), {"scope": "global"})]
