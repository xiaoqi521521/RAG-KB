from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.registry import REGISTRY

logger = logging.getLogger(__name__)


def _metric_or_existing(factory: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """应用测试/重载重复创建时复用默认 registry 的同名 collector。"""
    try:
        return factory(name, *args, **kwargs)  # type: ignore[operator]
    except ValueError:
        registry = kwargs.get("registry")
        if registry is not REGISTRY:
            raise
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if existing is None:
            raise
        return existing

TOKEN_REDIS_NAMESPACE = "rag:token:v3:"
_BUDGET_KEY_PREFIX = f"{TOKEN_REDIS_NAMESPACE}budget:"
_CHECK_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if redis.call('EXISTS', KEYS[1]) == 0 then
  redis.call('SET', KEYS[1], '0', 'EX', ARGV[2])
end
if current >= tonumber(ARGV[1]) then
  return -1
end
return current
"""


@dataclass
class _RequestTokenAccumulator:
    """保存一次请求的可共享 Token 累计，供后台观测任务继续更新。"""

    total: int = 0
    over_limit_recorded: bool = False


_REQUEST_TOKENS: ContextVar[_RequestTokenAccumulator | None] = ContextVar(
    "rag_request_token_total", default=None
)


class TokenBudgetRedis(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    async def incrby(self, name: Any, amount: int) -> Any: ...

    async def expire(self, name: Any, time: Any) -> Any: ...


class TokenBudgetExhaustedError(RuntimeError):
    """当天全局 Token 预算已耗尽。"""


class TokenBudgetUnavailableError(RuntimeError):
    """预算 Redis 状态不可读取或不可判断。"""


class GlobalTokenBudgetGate:
    """按部署时区执行全局每日 Token 预算检查和累计。"""

    def __init__(
        self,
        *,
        redis_client: TokenBudgetRedis,
        daily_budget: int = 1_000_000,
        timezone: str = "Asia/Shanghai",
        request_limit: int = 20_000,
        timeout_seconds: float = 1.0,
        registry: CollectorRegistry | None = None,
    ) -> None:
        if daily_budget <= 0:
            raise ValueError("daily_budget must be positive")
        if request_limit <= 0:
            raise ValueError("request_limit must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            self.timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown token budget timezone: {timezone}") from exc

        self.redis = redis_client
        self.daily_budget = daily_budget
        self.request_limit = request_limit
        self.timeout_seconds = timeout_seconds
        self.registry = registry or REGISTRY
        self._budget_used = _metric_or_existing(
            Gauge,
            "rag_token_budget_used_tokens",
            "Current global daily Token budget usage",
            ["scope"],
            registry=self.registry,
        )
        self._budget_limit = _metric_or_existing(
            Gauge,
            "rag_token_budget_limit_tokens",
            "Configured global daily Token budget",
            ["scope"],
            registry=self.registry,
        )
        self._budget_limit.labels(scope="global").set(daily_budget)
        self._rejected = _metric_or_existing(
            Counter,
            "rag_token_budget_rejected_total",
            "Requests rejected by the global Token budget",
            ["reason"],
            registry=self.registry,
        )
        self._request_tokens = _metric_or_existing(
            Histogram,
            "rag_token_request_tokens",
            "Observed total Tokens for one completed request",
            registry=self.registry,
        )
        self._request_over_limit = _metric_or_existing(
            Counter,
            "rag_token_request_over_limit_total",
            "Completed requests over the configured Token threshold",
            registry=self.registry,
        )
        self._write_failure = _metric_or_existing(
            Counter,
            "rag_token_write_failure_total",
            "Token usage sink write failures",
            ["sink", "token_type"],
            registry=self.registry,
        )

    def current_key(self, now: datetime | None = None) -> str:
        """返回当前部署时区下的每日预算 Redis key。"""
        effective_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        return f"{_BUDGET_KEY_PREFIX}{effective_now:%Y-%m-%d}"

    async def ensure_available(self) -> None:
        """原子检查预算，未耗尽才允许启动新的 provider 调用。"""
        key = self.current_key()
        ttl_seconds = self._seconds_until_next_day()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                current = int(
                    await self.redis.eval(
                        _CHECK_SCRIPT,
                        1,
                        key,
                        self.daily_budget,
                        max(ttl_seconds, 60),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            raise TokenBudgetUnavailableError("Token 预算状态暂不可用") from exc

        if current < 0:
            self._rejected.labels(reason="daily_budget_exhausted").inc()
            raise TokenBudgetExhaustedError("今日 Token 预算已用尽")
        self._budget_used.labels(scope="global").set(current)

    async def record_tokens(self, tokens: int) -> None:
        """把已完成 provider 调用的 Token 增量累计到当天全局计数。"""
        if tokens <= 0:
            return
        key = self.current_key()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                total = int(await self.redis.incrby(key, tokens))
                if total == tokens:
                    await self.redis.expire(key, max(self._seconds_until_next_day(), 60))
            self._budget_used.labels(scope="global").set(total)
        except Exception as exc:  # noqa: BLE001
            # 预算统计写失败不能回滚已经完成的模型调用。
            logger.warning(
                "Token budget usage write failed: error_type=%s",
                type(exc).__name__,
            )
            self._record_write_failure(sink="redis", token_type="budget")

    def add_request_tokens(self, tokens: int) -> None:
        """累加当前业务请求的内存 Token 总量，不保存请求级明细。"""
        accumulator = _REQUEST_TOKENS.get()
        if accumulator is None or tokens <= 0:
            return
        accumulator.total += tokens
        if accumulator.total > self.request_limit and not accumulator.over_limit_recorded:
            accumulator.over_limit_recorded = True
            self._request_over_limit.inc()
            logger.warning("token_request_over_limit=true")

    @asynccontextmanager
    async def request_scope(self) -> AsyncIterator[None]:
        """观察一次请求总量，并在超过阈值时只产生告警观测。"""
        accumulator = _RequestTokenAccumulator()
        token = _REQUEST_TOKENS.set(accumulator)
        try:
            yield
        finally:
            self._request_tokens.observe(accumulator.total)
            _REQUEST_TOKENS.reset(token)

    def _seconds_until_next_day(self) -> int:
        now = datetime.now(self.timezone)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((tomorrow - now).total_seconds()))

    def _record_write_failure(self, *, sink: str, token_type: str) -> None:
        """记录预算统计出口故障，指标自身故障也不能影响业务请求。"""
        try:
            self._write_failure.labels(sink=sink, token_type=token_type).inc()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Token budget write failure metric unavailable: sink=%s token_type=%s",
                sink,
                token_type,
            )
