from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families

from app.core.context import CurrentUser, current_user_var
from app.services.token_metrics import (
    TokenUsageRecorder,
    TokenMetricsUnavailableError,
    extract_generation_tokens,
    extract_input_tokens,
    extract_usage,
    record_generation_usage,
)


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.fail = False
        self.delay_seconds = 0.0
        self.hash_values: dict[str, str] = {}
        self.hgetall_calls = 0

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> list[object]:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("redis unavailable")
        name, token_field, raw_tokens, cost_field, raw_cost, type_cost_field, raw_type_cost = keys_and_args
        tokens = int(raw_tokens)
        cost = Decimal(str(raw_cost))
        type_cost = Decimal(str(raw_type_cost))
        self.calls.append((str(name), str(token_field), tokens))
        token_total = int(self.hash_values.get(str(token_field), "0")) + tokens
        cost_total = Decimal(self.hash_values.get(str(cost_field), "0")) + cost
        type_cost_total = Decimal(self.hash_values.get(str(type_cost_field), "0")) + type_cost
        self.hash_values[str(token_field)] = str(token_total)
        self.hash_values[str(cost_field)] = str(cost_total)
        self.hash_values[str(type_cost_field)] = str(type_cost_total)
        return [token_total, str(cost_total)]

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.calls.append((name, key, amount))
        return amount

    async def hgetall(self, name: str) -> dict[str, str]:
        self.hgetall_calls += 1
        if self.fail:
            raise RuntimeError("redis unavailable")
        return self.hash_values.copy()


def _build_recorder() -> tuple[TokenUsageRecorder, FakeRedis]:
    redis = FakeRedis()
    return TokenUsageRecorder(redis_client=redis, registry=CollectorRegistry()), redis


def _metric_values(recorder: TokenUsageRecorder, family_name: str) -> list[object]:
    families = {
        family.name: family for family in text_string_to_metric_families(recorder.render_metrics())
    }
    return families[family_name].samples if family_name in families else []


@pytest.mark.asyncio
async def test_record_chat_usage_writes_v2_and_allowed_labels() -> None:
    redis = FakeRedis()
    recorder = TokenUsageRecorder(
        redis_client=redis,
        registry=CollectorRegistry(),
        chat_input_price=Decimal("0.001"),
        chat_output_price=Decimal("0.002"),
    )
    token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="ADMIN"))
    try:
        await recorder.record_chat_usage(
            response=AIMessage(
                content="answer",
                usage_metadata={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128},
            ),
            model="deepseek-v4-flash",
            output_type="answer_generation",
            kb_id="multi",
        )
    finally:
        current_user_var.reset(token)

    assert redis.calls == [
        ("rag:token:v3:stats:7", "inputTokens", 120),
        ("rag:token:v3:stats:7", "answerGenerationTokens", 8),
    ]
    assert Decimal(redis.hash_values["estimatedCostCny"]) == Decimal("0.000136")
    assert Decimal(redis.hash_values["inputCostCny"]) == Decimal("0.00012")
    assert Decimal(redis.hash_values["answerGenerationCostCny"]) == Decimal("0.000016")
    usage = [sample for sample in _metric_values(recorder, "rag_token_usage") if sample.name.endswith("_total")]
    assert {
        (sample.labels["model"], sample.labels["token_type"], sample.labels["kb_id"]): sample.value
        for sample in usage
    } == {
        ("deepseek-v4-flash", "input", "multi"): 120.0,
        ("deepseek-v4-flash", "answer_generation", "multi"): 8.0,
    }
    assert all(set(sample.labels) == {"model", "token_type", "kb_id"} for sample in usage)


@pytest.mark.asyncio
async def test_record_chat_usage_records_intent_output_bucket() -> None:
    redis = FakeRedis()
    recorder = TokenUsageRecorder(
        redis_client=redis,
        registry=CollectorRegistry(),
        chat_input_price=Decimal("0.001"),
        chat_output_price=Decimal("0.002"),
    )
    token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="ADMIN"))
    try:
        await recorder.record_chat_usage(
            response=AIMessage(
                content='{"intent":"GENERAL_CHAT"}',
                usage_metadata={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128},
            ),
            model="deepseek-v4-flash",
            output_type="intent",
            kb_id="multi",
        )
    finally:
        current_user_var.reset(token)

    assert redis.calls == [
        ("rag:token:v3:stats:7", "inputTokens", 120),
        ("rag:token:v3:stats:7", "intentTokens", 8),
    ]
    assert Decimal(redis.hash_values["estimatedCostCny"]) == Decimal("0.000136")
    assert Decimal(redis.hash_values["intentCostCny"]) == Decimal("0.000016")
    usage = [sample for sample in _metric_values(recorder, "rag_token_usage") if sample.name.endswith("_total")]
    assert {
        (sample.labels["model"], sample.labels["token_type"], sample.labels["kb_id"]): sample.value
        for sample in usage
    } == {
        ("deepseek-v4-flash", "input", "multi"): 120.0,
        ("deepseek-v4-flash", "intent", "multi"): 8.0,
    }


@pytest.mark.asyncio
async def test_offline_embedding_does_not_write_user_v2() -> None:
    recorder, redis = _build_recorder()
    token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="ADMIN"))
    try:
        await recorder.record_usage(
            tokens=17,
            model="text-embedding-v3",
            token_type="embedding",
            kb_id=2,
            user_scoped=False,
            budget_scoped=False,
        )
    finally:
        current_user_var.reset(token)

    assert redis.calls == []
    samples = [sample for sample in _metric_values(recorder, "rag_token_usage") if sample.name.endswith("_total")]
    assert samples[0].labels == {"model": "text-embedding-v3", "token_type": "embedding", "kb_id": "2"}


@pytest.mark.asyncio
async def test_redis_write_failure_does_not_break_prometheus_recording() -> None:
    recorder, redis = _build_recorder()
    redis.fail = True
    token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="ADMIN"))
    try:
        await recorder.record_usage(
            tokens=5,
            model="deepseek-v4-flash",
            token_type="input",
            kb_id=2,
        )
    finally:
        current_user_var.reset(token)

    assert any(sample.value == 5 for sample in _metric_values(recorder, "rag_token_usage"))
    assert any(sample.labels["sink"] == "redis" for sample in _metric_values(recorder, "rag_token_write_failure"))


@pytest.mark.asyncio
async def test_redis_write_timeout_does_not_break_prometheus_recording() -> None:
    recorder, redis = _build_recorder()
    recorder.write_timeout_seconds = 0.001
    redis.delay_seconds = 0.01
    token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="ADMIN"))
    try:
        await recorder.record_usage(
            tokens=5,
            model="deepseek-v4-flash",
            token_type="input",
            kb_id=2,
        )
    finally:
        current_user_var.reset(token)

    assert any(sample.value == 5 for sample in _metric_values(recorder, "rag_token_usage"))
    assert any(sample.labels["sink"] == "redis" for sample in _metric_values(recorder, "rag_token_write_failure"))


def test_usage_extraction_supports_langchain_and_openai_metadata() -> None:
    response = AIMessage(
        content="answer",
        usage_metadata={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
    )
    assert extract_input_tokens(response) == 12
    assert extract_generation_tokens(response) == 8
    assert extract_usage(SimpleNamespace(response_metadata={"token_usage": {"prompt_tokens": 9, "completion_tokens": 4}})) == (9, 4)


def test_invalid_or_missing_usage_is_not_estimated() -> None:
    assert extract_usage(SimpleNamespace(usage_metadata={"input_tokens": -1, "output_tokens": "bad"})) == (None, None)
    assert extract_usage(SimpleNamespace()) == (None, None)


@pytest.mark.asyncio
async def test_missing_generation_usage_logs_unavailable_signal(caplog: pytest.LogCaptureFixture) -> None:
    class FakeRecorder:
        async def record_generation_tokens(self, *, tokens: int, source: str = "provider") -> None:
            raise AssertionError("missing usage must not record a value")

    with caplog.at_level(logging.INFO, logger="app.services.token_metrics"):
        await record_generation_usage(
            recorder=FakeRecorder(),
            response=AIMessage(content="answer"),
            pipeline="v4",
        )

    assert "token_usage_unavailable=true" in caplog.text


@pytest.mark.asyncio
async def test_read_user_tokens_uses_only_v2_fields_and_zero_defaults() -> None:
    recorder, redis = _build_recorder()
    redis.hash_values = {
        "embeddingTokens": "125",
        "inputTokens": "890",
        "answerGenerationTokens": "8",
        "intentTokens": "5",
        "estimatedCostCny": "0.0125",
        "embeddingCostCny": "0.0012",
        "inputCostCny": "0.0020",
        "answerGenerationCostCny": "0.0008",
        "intentCostCny": "0.0001",
        "hydeCostCny": "0.0004",
        "rerankerCostCny": "0.0003",
        "faithfulnessCostCny": "0.0002",
        "legacyGenerationTokens": "999",
    }

    usage = await recorder.read_user_tokens(7)

    assert usage.embedding_tokens == 125
    assert usage.input_tokens == 890
    assert usage.answer_generation_tokens == 8
    assert usage.intent_tokens == 5
    assert usage.hyde_tokens == 0
    assert usage.reranker_tokens == 0
    assert usage.faithfulness_tokens == 0
    assert usage.estimated_cost_cny == Decimal("0.0125")
    assert usage.embedding_cost_cny == Decimal("0.0012")
    assert usage.input_cost_cny == Decimal("0.0020")
    assert usage.answer_generation_cost_cny == Decimal("0.0008")
    assert usage.intent_cost_cny == Decimal("0.0001")
    assert usage.hyde_cost_cny == Decimal("0.0004")
    assert usage.reranker_cost_cny == Decimal("0.0003")
    assert usage.faithfulness_cost_cny == Decimal("0.0002")


@pytest.mark.asyncio
async def test_read_user_tokens_rejects_unreadable_redis_data() -> None:
    recorder, redis = _build_recorder()
    redis.hash_values = {"inputTokens": "not-a-number"}
    with pytest.raises(TokenMetricsUnavailableError):
        await recorder.read_user_tokens(7)

    redis.fail = True
    with pytest.raises(TokenMetricsUnavailableError):
        await recorder.read_user_tokens(7)
    assert redis.hgetall_calls == 3
