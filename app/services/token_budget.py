from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncIterator, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Meter, Observation

logger = logging.getLogger(__name__)


TOKEN_REDIS_NAMESPACE = "rag:token:v3:"
_METER_NAME = "rag-kb.token-metrics"
# 金额预算使用独立子版本，避免把旧 Token 预算值解释成 CNY。
_BUDGET_KEY_PREFIX = f"{TOKEN_REDIS_NAMESPACE}budget:cny:"
_CHECK_SCRIPT = """
local raw_current = redis.call('GET', KEYS[1])
local current = tonumber(raw_current or '0')
if current == nil then
  return '-2'
end
if redis.call('EXISTS', KEYS[1]) == 0 then
  redis.call('SET', KEYS[1], '0')
end
if current >= tonumber(ARGV[1]) then
  return '-1'
end
-- Redis 会把 Lua 数值返回截断为整数，金额必须以字符串返回才能保留小数。
return tostring(current)
"""


def _as_decimal(value: Decimal | int | float | str) -> Decimal:
    """把配置或调用方传入的金额统一为 Decimal，避免二进制浮点误差。"""
    return Decimal(str(value))


@dataclass
class _RequestCostAccumulator:
    """保存一次请求的可共享金额累计，供后台观测任务继续更新。"""

    total: Decimal = Decimal("0")
    over_limit_recorded: bool = False


_REQUEST_COST: ContextVar[_RequestCostAccumulator | None] = ContextVar(
    "rag_request_cost_total", default=None
)


class TokenBudgetRedis(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    async def incrbyfloat(self, name: Any, amount: Any) -> Any: ...


class TokenBudgetExhaustedError(RuntimeError):
    """当天全局金额预算已耗尽。"""


class TokenBudgetUnavailableError(RuntimeError):
    """预算 Redis 状态不可读取或不可判断。"""


class GlobalTokenBudgetGate:
    """按部署时区执行全局每日金额预算检查和累计。"""

    def __init__(
        self,
        *,
        redis_client: TokenBudgetRedis,
        daily_budget_cny: Decimal | int | float | str = Decimal("1.00"),
        timezone: str = "Asia/Shanghai",
        request_cost_limit_cny: Decimal | int | float | str = Decimal("0.01"),
        timeout_seconds: float = 1.0,
        meter: Meter | None = None,
    ) -> None:
        daily_budget = _as_decimal(daily_budget_cny)
        request_cost_limit = _as_decimal(request_cost_limit_cny)
        if not daily_budget.is_finite() or daily_budget <= 0:
            raise ValueError("daily_budget_cny must be a positive finite Decimal")
        if not request_cost_limit.is_finite() or request_cost_limit <= 0:
            raise ValueError("request_cost_limit_cny must be a positive finite Decimal")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            self.timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown token budget timezone: {timezone}") from exc

        self.redis = redis_client
        self.daily_budget_cny = daily_budget
        self.request_cost_limit_cny = request_cost_limit
        self.timeout_seconds = timeout_seconds
        effective_meter = meter or metrics.get_meter(_METER_NAME)
        # 同步 Gauge 每次采集即消费，空闲期会导致 Prometheus 序列消失；
        # 改用 ObservableGauge，每次抓取回调重新产出当前值。
        self._budget_used_value = 0.0
        self._budget_used = effective_meter.create_observable_gauge(
            "rag_token_budget_used_cny",
            description="Current global daily CNY budget usage",
            callbacks=[self._observe_budget_used],
        )
        self._budget_limit_value = float(daily_budget)
        self._budget_limit = effective_meter.create_observable_gauge(
            "rag_token_budget_limit_cny",
            description="Configured global daily CNY budget",
            callbacks=[self._observe_budget_limit],
        )
        self._rejected = effective_meter.create_counter(
            "rag_token_budget_rejected",
            description="Requests rejected by the global CNY budget",
        )
        self._request_cost = effective_meter.create_histogram(
            "rag_token_request_cost_cny",
            description="Observed CNY cost for one completed request",
        )
        self._request_over_limit = effective_meter.create_counter(
            "rag_token_request_cost_over_limit",
            description="Completed requests over the configured CNY threshold",
        )
        # write failure 供 TokenUsageRecorder 共用，避免同名指标重复导出。
        self.write_failure_counter = effective_meter.create_counter(
            "rag_token_write_failure",
            description="Token usage sink write failures",
        )
        self._write_failure = self.write_failure_counter

    def _observe_budget_used(self, options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(self._budget_used_value, {"scope": "global"})

    def _observe_budget_limit(self, options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(self._budget_limit_value, {"scope": "global"})

    def current_key(self, now: datetime | None = None) -> str:
        """返回当前部署时区下的每日预算 Redis key。"""
        effective_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        # key 中间段携带年份和月份，且每日 key 永久保留（不设置 TTL），
        # 便于在 Redis 中按年月回溯历史花费。
        return f"{_BUDGET_KEY_PREFIX}{effective_now:%Y:%m:%Y-%m-%d}"

    async def ensure_available(self) -> None:
        """原子检查预算，未耗尽才允许启动新的 provider 调用。"""
        key = self.current_key()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                current = _as_decimal(
                    await self.redis.eval(
                        _CHECK_SCRIPT,
                        1,
                        key,
                        str(self.daily_budget_cny),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            raise TokenBudgetUnavailableError("金额预算状态暂不可用") from exc

        if current == Decimal("-2"):
            raise TokenBudgetUnavailableError("金额预算数据不可用")
        if current < Decimal("0"):
            self._rejected.add(1, {"reason": "daily_budget_exhausted"})
            raise TokenBudgetExhaustedError("今日金额预算已用尽")
        self._budget_used_value = float(current)

    async def record_cost(self, cost_cny: Decimal | int | float | str) -> None:
        """把已完成 provider 调用的金额增量累计到当天全局计数。"""
        cost = _as_decimal(cost_cny)
        if not cost.is_finite() or cost <= 0:
            return
        key = self.current_key()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                total = _as_decimal(await self.redis.incrbyfloat(key, str(cost)))
            self._budget_used_value = float(total)
        except Exception as exc:  # noqa: BLE001
            # 预算统计写失败不能回滚已经完成的模型调用。
            logger.warning(
                "CNY budget usage write failed: error_type=%s",
                type(exc).__name__,
            )
            self._record_write_failure(sink="redis", token_type="budget")

    def add_request_cost(self, cost_cny: Decimal | int | float | str) -> None:
        """累加当前业务请求的内存金额，不保存请求级明细。"""
        accumulator = _REQUEST_COST.get()
        cost = _as_decimal(cost_cny)
        if accumulator is None or not cost.is_finite() or cost <= 0:
            return
        accumulator.total += cost
        if (
            accumulator.total > self.request_cost_limit_cny
            and not accumulator.over_limit_recorded
        ):
            accumulator.over_limit_recorded = True
            self._request_over_limit.add(1)
            logger.warning("request_cost_over_limit=true")

    @asynccontextmanager
    async def request_scope(self) -> AsyncIterator[None]:
        """观察一次请求总量，并在超过阈值时只产生告警观测。"""
        accumulator = _RequestCostAccumulator()
        token = _REQUEST_COST.set(accumulator)
        try:
            yield
        finally:
            self._request_cost.record(float(accumulator.total))
            _REQUEST_COST.reset(token)

    def _record_write_failure(self, *, sink: str, token_type: str) -> None:
        """记录预算统计出口故障，指标自身故障也不能影响业务请求。"""
        try:
            self._write_failure.add(1, {"sink": sink, "token_type": token_type})
        except Exception:  # noqa: BLE001
            logger.warning(
                "Token budget write failure metric unavailable: sink=%s token_type=%s",
                sink,
                token_type,
            )
