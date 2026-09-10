from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.services.token_budget import (
    _CHECK_SCRIPT,
    GlobalTokenBudgetGate,
    TokenBudgetExhaustedError,
    TokenBudgetUnavailableError,
)


class FakeRedis:
    def __init__(self, check_result: object = 0) -> None:
        self.check_result = check_result
        self.eval_calls: list[tuple[object, ...]] = []
        self.incrbyfloat_calls: list[tuple[str, str]] = []
        self.value = Decimal("0")
        self.expire_calls: list[tuple[str, int]] = []
        self.fail = False

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.eval_calls.append((script, numkeys, *keys_and_args))
        return self.check_result

    async def incrbyfloat(self, name: str, amount: str) -> str:
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.incrbyfloat_calls.append((name, amount))
        self.value += Decimal(amount)
        return str(self.value)

    async def expire(self, name: str, time: int) -> bool:
        self.expire_calls.append((name, time))
        return True


class SlowRedis(FakeRedis):
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        await asyncio.sleep(0.01)
        return await super().eval(script, numkeys, *keys_and_args)


class SlowIncrementRedis(FakeRedis):
    async def incrbyfloat(self, name: str, amount: str) -> str:
        await asyncio.sleep(0.01)
        return await super().incrbyfloat(name, amount)


def _build_metrics() -> tuple[MeterProvider, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    return MeterProvider(metric_readers=[reader]), reader


def _data_points(reader: InMemoryMetricReader, metric_name: str) -> list[object]:
    metrics_data = reader.get_metrics_data()
    return [
        data_point
        for resource in metrics_data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == metric_name
        for data_point in metric.data.data_points
    ]


def test_check_script_returns_usage_as_string_to_avoid_lua_truncation() -> None:
    """Redis 会把 Lua 数值返回截断为整数，预算金额必须以字符串读回。"""
    assert "return tostring(current)" in _CHECK_SCRIPT
    assert "return '-1'" in _CHECK_SCRIPT
    # 每日预算 key 永久保留供回溯历史花费，脚本不得再写入过期时间。
    assert "'EX'" not in _CHECK_SCRIPT


@pytest.mark.asyncio
async def test_budget_uses_asia_shanghai_daily_key_and_allows_below_limit() -> None:
    redis = FakeRedis(check_result=0)
    provider, _ = _build_metrics()
    gate = GlobalTokenBudgetGate(
        redis_client=redis,
        daily_budget_cny=Decimal("1.00"),
        meter=provider.get_meter("tests"),
    )

    assert gate.current_key(datetime(2026, 7, 22, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))) == (
        "rag:token:v3:budget:cny:2026:07:2026-07-22"
    )
    await gate.ensure_available()

    assert redis.eval_calls[0][2] == gate.current_key()
    assert redis.eval_calls[0][3] == "1.00"


@pytest.mark.asyncio
async def test_budget_preserves_decimal_usage_from_redis() -> None:
    redis = FakeRedis(check_result="0.25")
    provider, reader = _build_metrics()
    gate = GlobalTokenBudgetGate(redis_client=redis, meter=provider.get_meter("tests"))

    await gate.ensure_available()

    ratio = _data_points(reader, "rag_token_budget_usage_ratio")
    assert ratio[0].value == 0.25


@pytest.mark.asyncio
async def test_budget_rejects_when_redis_check_reports_exhaustion() -> None:
    provider, _ = _build_metrics()
    gate = GlobalTokenBudgetGate(
        redis_client=FakeRedis(check_result=-1),
        meter=provider.get_meter("tests"),
    )

    with pytest.raises(TokenBudgetExhaustedError):
        await gate.ensure_available()


@pytest.mark.asyncio
async def test_budget_redis_failure_is_unavailable() -> None:
    provider, _ = _build_metrics()
    gate = GlobalTokenBudgetGate(redis_client=FakeRedis(), meter=provider.get_meter("tests"))
    gate.redis.fail = True  # type: ignore[attr-defined]

    with pytest.raises(TokenBudgetUnavailableError):
        await gate.ensure_available()


@pytest.mark.asyncio
async def test_budget_redis_timeout_is_unavailable() -> None:
    provider, _ = _build_metrics()
    gate = GlobalTokenBudgetGate(
        redis_client=SlowRedis(),
        timeout_seconds=0.001,
        meter=provider.get_meter("tests"),
    )

    with pytest.raises(TokenBudgetUnavailableError):
        await gate.ensure_available()


@pytest.mark.asyncio
async def test_budget_check_failure_is_not_recorded_as_write_failure() -> None:
    provider, reader = _build_metrics()
    gate = GlobalTokenBudgetGate(
        redis_client=SlowRedis(),
        timeout_seconds=0.001,
        meter=provider.get_meter("tests"),
    )

    with pytest.raises(TokenBudgetUnavailableError):
        await gate.ensure_available()

    assert _data_points(reader, "rag_token_write_failure") == []


@pytest.mark.asyncio
async def test_budget_record_failure_is_non_blocking_and_observable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = FakeRedis()
    redis.fail = True
    redis.connection_pool = type(
        "FakeConnectionPool",
        (),
        {
            "connection_kwargs": {
                "host": "redis-host",
                "port": 6379,
                "db": 2,
                "password": "super-secret",
                "url": "redis://:super-secret@redis-host:6379/2",
            }
        },
    )()
    provider, reader = _build_metrics()
    gate = GlobalTokenBudgetGate(redis_client=redis, meter=provider.get_meter("tests"))

    with caplog.at_level(logging.WARNING, logger="app.services.token_budget"):
        await gate.record_cost(Decimal("0.10"))

    assert "CNY budget usage write failed" in caplog.text
    assert "cost_cny=0.10" in caplog.text
    assert "timeout_seconds=1.0" in caplog.text
    assert "redis=host=redis-host port=6379 db=2" in caplog.text
    assert f"key={gate.current_key()}" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "redis unavailable" in caplog.text
    assert "super-secret" not in caplog.text
    assert "redis://:super-secret" not in caplog.text

    await gate.record_cost(Decimal("0.10"))

    write_failures = _data_points(reader, "rag_token_write_failure")
    assert any(
        data_point.attributes == {"sink": "redis", "component": "budget"}
        and data_point.value == 2
        for data_point in write_failures
    )


@pytest.mark.asyncio
async def test_budget_record_timeout_is_not_confirmed_write_failure() -> None:
    provider, reader = _build_metrics()
    gate = GlobalTokenBudgetGate(
        redis_client=SlowIncrementRedis(),
        timeout_seconds=0.001,
        meter=provider.get_meter("tests"),
    )

    await gate.record_cost(Decimal("0.10"))

    assert _data_points(reader, "rag_token_write_failure") == []


@pytest.mark.asyncio
async def test_budget_record_stores_cost_in_parallel_budget_key() -> None:
    redis = FakeRedis()
    provider, _ = _build_metrics()
    gate = GlobalTokenBudgetGate(redis_client=redis, meter=provider.get_meter("tests"))

    await gate.record_cost(Decimal("0.25"))

    assert redis.incrbyfloat_calls == [(gate.current_key(), "0.25")]
    assert redis.expire_calls == []
    assert redis.value == Decimal("0.25")


@pytest.mark.asyncio
async def test_request_scope_marks_completed_request_over_limit_without_rejecting_it() -> None:
    provider, reader = _build_metrics()
    gate = GlobalTokenBudgetGate(
        redis_client=FakeRedis(),
        request_cost_limit_cny=Decimal("0.01"),
        meter=provider.get_meter("tests"),
    )

    async with gate.request_scope():
        gate.add_request_cost(Decimal("0.0101"))

    over_limit = _data_points(reader, "rag_token_request_cost_over_limit")
    assert any(
        data_point.value == 1 for data_point in over_limit
    )


@pytest.mark.asyncio
async def test_request_scope_does_not_alert_at_the_cost_limit() -> None:
    provider, reader = _build_metrics()
    gate = GlobalTokenBudgetGate(
        redis_client=FakeRedis(),
        request_cost_limit_cny=Decimal("0.01"),
        meter=provider.get_meter("tests"),
    )

    async with gate.request_scope():
        gate.add_request_cost(Decimal("0.01"))

    over_limit = _data_points(reader, "rag_token_request_cost_over_limit")
    assert not any(
        data_point.value == 1 for data_point in over_limit
    )


@pytest.mark.asyncio
async def test_background_token_update_shares_request_limit_observation() -> None:
    provider, reader = _build_metrics()
    gate = GlobalTokenBudgetGate(
        redis_client=FakeRedis(),
        request_cost_limit_cny=Decimal("0.01"),
        meter=provider.get_meter("tests"),
    )

    async with gate.request_scope():
        task = asyncio.create_task(_add_request_cost(gate, Decimal("0.0101")))
        await task

    over_limit = _data_points(reader, "rag_token_request_cost_over_limit")
    assert any(
        data_point.value == 1 for data_point in over_limit
    )


async def _add_request_cost(gate: GlobalTokenBudgetGate, cost_cny: Decimal) -> None:
    gate.add_request_cost(cost_cny)
