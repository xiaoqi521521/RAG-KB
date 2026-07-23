from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from prometheus_client import CollectorRegistry

from app.services.token_budget import (
    GlobalTokenBudgetGate,
    TokenBudgetExhaustedError,
    TokenBudgetUnavailableError,
)


class FakeRedis:
    def __init__(self, check_result: int = 0) -> None:
        self.check_result = check_result
        self.eval_calls: list[tuple[object, ...]] = []
        self.incrby_calls: list[tuple[str, int]] = []
        self.value = 0
        self.expire_calls: list[tuple[str, int]] = []
        self.fail = False

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.eval_calls.append((script, numkeys, *keys_and_args))
        return self.check_result

    async def incrby(self, name: str, amount: int) -> int:
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.incrby_calls.append((name, amount))
        self.value += amount
        return self.value

    async def expire(self, name: str, time: int) -> bool:
        self.expire_calls.append((name, time))
        return True


class SlowRedis(FakeRedis):
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        await asyncio.sleep(0.01)
        return await super().eval(script, numkeys, *keys_and_args)


@pytest.mark.asyncio
async def test_budget_uses_asia_shanghai_daily_key_and_allows_below_limit() -> None:
    redis = FakeRedis(check_result=799_999)
    gate = GlobalTokenBudgetGate(
        redis_client=redis,
        daily_budget=1_000_000,
        registry=CollectorRegistry(),
    )

    assert gate.current_key(datetime(2026, 7, 22, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))) == (
        "rag:token:v3:budget:2026-07-22"
    )
    await gate.ensure_available()

    assert redis.eval_calls[0][2] == gate.current_key()
    assert redis.eval_calls[0][3] == 1_000_000


@pytest.mark.asyncio
async def test_budget_rejects_when_redis_check_reports_exhaustion() -> None:
    gate = GlobalTokenBudgetGate(
        redis_client=FakeRedis(check_result=-1),
        registry=CollectorRegistry(),
    )

    with pytest.raises(TokenBudgetExhaustedError):
        await gate.ensure_available()


@pytest.mark.asyncio
async def test_budget_redis_failure_is_unavailable() -> None:
    gate = GlobalTokenBudgetGate(redis_client=FakeRedis(), registry=CollectorRegistry())
    gate.redis.fail = True  # type: ignore[attr-defined]

    with pytest.raises(TokenBudgetUnavailableError):
        await gate.ensure_available()


@pytest.mark.asyncio
async def test_budget_redis_timeout_is_unavailable() -> None:
    gate = GlobalTokenBudgetGate(
        redis_client=SlowRedis(),
        timeout_seconds=0.001,
        registry=CollectorRegistry(),
    )

    with pytest.raises(TokenBudgetUnavailableError):
        await gate.ensure_available()


@pytest.mark.asyncio
async def test_budget_record_failure_is_non_blocking_and_observable() -> None:
    redis = FakeRedis()
    redis.fail = True
    gate = GlobalTokenBudgetGate(redis_client=redis, registry=CollectorRegistry())

    await gate.record_tokens(10)

    metrics = gate.registry.collect()
    assert any(
        sample.name == "rag_token_write_failure_total"
        and sample.labels == {"sink": "redis", "token_type": "budget"}
        and sample.value == 1
        for family in metrics
        for sample in family.samples
    )


@pytest.mark.asyncio
async def test_budget_record_stores_token_count_in_parallel_budget_key() -> None:
    redis = FakeRedis()
    gate = GlobalTokenBudgetGate(redis_client=redis, registry=CollectorRegistry())

    await gate.record_tokens(2_520)

    assert redis.incrby_calls == [(gate.current_key(), 2_520)]
    assert redis.value == 2_520


@pytest.mark.asyncio
async def test_request_scope_marks_completed_request_over_limit_without_rejecting_it() -> None:
    gate = GlobalTokenBudgetGate(
        redis_client=FakeRedis(),
        request_limit=20_000,
        registry=CollectorRegistry(),
    )

    async with gate.request_scope():
        gate.add_request_tokens(20_001)

    metrics = gate.registry.collect()
    assert any(
        sample.name == "rag_token_request_over_limit_total" and sample.value == 1
        for family in metrics
        for sample in family.samples
    )


@pytest.mark.asyncio
async def test_background_token_update_shares_request_limit_observation() -> None:
    gate = GlobalTokenBudgetGate(
        redis_client=FakeRedis(),
        request_limit=20_000,
        registry=CollectorRegistry(),
    )

    async with gate.request_scope():
        task = asyncio.create_task(_add_tokens(gate, 20_001))
        await task

    metrics = gate.registry.collect()
    assert any(
        sample.name == "rag_token_request_over_limit_total" and sample.value == 1
        for family in metrics
        for sample in family.samples
    )


async def _add_tokens(gate: GlobalTokenBudgetGate, tokens: int) -> None:
    gate.add_request_tokens(tokens)
