from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.core.context import CurrentUser, current_user_var
from app.services.token_metrics import (
    evaluation_metrics_scope_var,
    suppress_user_usage_var,
    suppress_daily_usage_var,
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
        self.daily_calls: list[tuple[str, str, int]] = []
        self.fail = False
        self.delay_seconds = 0.0
        self.hash_values: dict[str, str] = {}
        self.daily_hash_values: dict[str, dict[str, int]] = {}
        self.hgetall_calls = 0

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> list[object]:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("redis unavailable")
        name, token_field, raw_tokens = keys_and_args
        tokens = int(raw_tokens)
        self.calls.append((str(name), str(token_field), tokens))
        token_total = int(self.hash_values.get(str(token_field), "0")) + tokens
        self.hash_values[str(token_field)] = str(token_total)
        return [token_total]

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("redis unavailable")
        name = str(name)
        key = str(key)
        values = self.daily_hash_values.setdefault(name, {})
        values[key] = values.get(key, 0) + amount
        self.daily_calls.append((name, key, amount))
        return values[key]

    async def hgetall(self, name: str) -> dict[str, str]:
        self.hgetall_calls += 1
        if self.fail:
            raise RuntimeError("redis unavailable")
        if str(name).startswith("rag:token:v3:daily:"):
            return self.daily_hash_values.get(str(name), {}).copy()
        return self.hash_values.copy()


def _build_recorder() -> tuple[TokenUsageRecorder, FakeRedis, InMemoryMetricReader]:
    redis = FakeRedis()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return TokenUsageRecorder(redis_client=redis, meter=provider.get_meter("tests")), redis, reader


def _metric_values(reader: InMemoryMetricReader, metric_name: str) -> list[object]:
    metrics_data = reader.get_metrics_data()
    return [
        data_point
        for resource in metrics_data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == metric_name
        for data_point in metric.data.data_points
    ]


def _daily_usage_key(recorder: TokenUsageRecorder) -> str:
    return f"rag:token:v3:daily:{datetime.now(recorder.timezone):%Y:%m:%Y-%m-%d}"


@pytest.mark.asyncio
async def test_record_chat_usage_writes_v2_and_allowed_labels() -> None:
    redis = FakeRedis()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = TokenUsageRecorder(
        redis_client=redis,
        meter=provider.get_meter("tests"),
        chat_input_price=Decimal("0.001"),
        chat_output_price=Decimal("0.002"),
        embedding_price=Decimal("0.0005"),
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
    assert "estimatedCostCny" not in redis.hash_values
    usage = _metric_values(reader, "rag_token_usage")
    assert {
        (
            data_point.attributes["model"],
            data_point.attributes["token_type"],
            data_point.attributes["kb_id"],
        ): data_point.value
        for data_point in usage
    } == {
        ("deepseek-v4-flash", "input", "multi"): 120.0,
        ("deepseek-v4-flash", "answer_generation", "multi"): 8.0,
    }
    assert all(set(data_point.attributes) == {"model", "token_type", "kb_id"} for data_point in usage)


@pytest.mark.asyncio
async def test_record_chat_usage_records_intent_output_bucket() -> None:
    redis = FakeRedis()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = TokenUsageRecorder(
        redis_client=redis,
        meter=provider.get_meter("tests"),
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
    assert "estimatedCostCny" not in redis.hash_values
    usage = _metric_values(reader, "rag_token_usage")
    assert {
        (
            data_point.attributes["model"],
            data_point.attributes["token_type"],
            data_point.attributes["kb_id"],
        ): data_point.value
        for data_point in usage
    } == {
        ("deepseek-v4-flash", "input", "multi"): 120.0,
        ("deepseek-v4-flash", "intent", "multi"): 8.0,
    }


@pytest.mark.asyncio
async def test_ragas_evaluation_does_not_write_daily_hash() -> None:
    recorder, redis, reader = _build_recorder()
    scope_token = evaluation_metrics_scope_var.set(True)
    token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="ADMIN"))
    try:
        await recorder.record_usage(
            tokens=12,
            model="deepseek-v4-flash",
            token_type="ragas_evaluation",
            kb_id=3,
            user_scoped=False,
            daily_scoped=False,
        )
    finally:
        evaluation_metrics_scope_var.reset(scope_token)
        current_user_var.reset(token)

    assert redis.calls == []
    assert redis.daily_calls == []
    usage = _metric_values(reader, "rag_evaluation_token_usage")
    assert {
        (
            data_point.attributes["model"],
            data_point.attributes["token_type"],
            data_point.attributes["kb_id"],
        ): data_point.value
        for data_point in usage
    } == {
        ("deepseek-v4-flash", "ragas_evaluation", "3"): 12.0,
    }


@pytest.mark.asyncio
async def test_suppress_switch_skips_personal_redis_but_keeps_prometheus() -> None:
    recorder, redis, reader = _build_recorder()
    user_token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="USER"))
    suppress_token = suppress_user_usage_var.set(True)
    try:
        await recorder.record_usage(
            tokens=9,
            model="deepseek-v4-flash",
            token_type="input",
            kb_id=2,
        )
    finally:
        suppress_user_usage_var.reset(suppress_token)
        current_user_var.reset(user_token)

    assert redis.calls == []
    assert redis.daily_calls == [(_daily_usage_key(recorder), "inputTokens", 9)]
    samples = _metric_values(reader, "rag_token_usage")
    assert dict(samples[0].attributes) == {
        "model": "deepseek-v4-flash",
        "token_type": "input",
        "kb_id": "2",
    }


@pytest.mark.asyncio
async def test_evaluation_scope_routes_usage_to_evaluation_metrics() -> None:
    redis = FakeRedis()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = TokenUsageRecorder(
        redis_client=redis,
        meter=provider.get_meter("tests"),
        chat_input_price=Decimal("0.001"),
        chat_output_price=Decimal("0.002"),
        embedding_price=Decimal("0.0005"),
    )
    scope_token = evaluation_metrics_scope_var.set(True)
    try:
        await recorder.record_usage(
            tokens=100,
            model="deepseek-v4-flash",
            token_type="ragas_input",
            kb_id=3,
            user_scoped=False,
            daily_scoped=False,
        )
        await recorder.record_usage(
            tokens=40,
            model="deepseek-v4-flash",
            token_type="ragas_evaluation",
            kb_id=3,
            user_scoped=False,
            daily_scoped=False,
        )
        await recorder.record_usage(
            tokens=25,
            model="text-embedding-v3",
            token_type="ragas_embedding",
            kb_id=3,
            user_scoped=False,
            daily_scoped=False,
        )
    finally:
        evaluation_metrics_scope_var.reset(scope_token)

    assert _metric_values(reader, "rag_token_usage") == []
    assert _metric_values(reader, "rag_token_usage_cost_cny") == []
    usage = _metric_values(reader, "rag_evaluation_token_usage")
    assert [
        (
            data_point.attributes["model"],
            data_point.attributes["token_type"],
            data_point.attributes["kb_id"],
            data_point.value,
        )
        for data_point in usage
    ] == [
        ("deepseek-v4-flash", "ragas_input", "3", 100.0),
        ("deepseek-v4-flash", "ragas_evaluation", "3", 40.0),
        ("text-embedding-v3", "ragas_embedding", "3", 25.0),
    ]
    cost = _metric_values(reader, "rag_evaluation_token_usage_cost_cny")
    assert {data_point.value for data_point in cost} == {0.0001, 0.00008, 0.0000125}


@pytest.mark.asyncio
async def test_daily_suppression_skips_global_hash_but_keeps_personal_stats() -> None:
    recorder, redis, reader = _build_recorder()
    user_token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="USER"))
    suppress_token = suppress_daily_usage_var.set(True)
    try:
        await recorder.record_usage(
            tokens=9,
            model="deepseek-v4-flash",
            token_type="input",
            kb_id=2,
        )
    finally:
        suppress_daily_usage_var.reset(suppress_token)
        current_user_var.reset(user_token)

    assert redis.daily_calls == []
    assert redis.calls == [("rag:token:v3:stats:7", "inputTokens", 9)]
    samples = _metric_values(reader, "rag_token_usage")
    assert samples[0].value == 9.0


@pytest.mark.asyncio
async def test_offline_embedding_does_not_write_user_v2() -> None:
    recorder, redis, reader = _build_recorder()
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
    assert redis.daily_calls == [(_daily_usage_key(recorder), "embeddingTokens", 17)]
    samples = _metric_values(reader, "rag_token_usage")
    assert dict(samples[0].attributes) == {
        "model": "text-embedding-v3",
        "token_type": "embedding",
        "kb_id": "2",
    }


@pytest.mark.asyncio
async def test_redis_write_failure_does_not_break_prometheus_recording() -> None:
    recorder, redis, reader = _build_recorder()
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

    assert any(data_point.value == 5 for data_point in _metric_values(reader, "rag_token_usage"))
    assert any(
        data_point.attributes == {"sink": "redis", "component": "input"}
        for data_point in _metric_values(reader, "rag_token_write_failure")
    )


@pytest.mark.asyncio
async def test_redis_write_timeout_is_not_confirmed_failure() -> None:
    recorder, redis, reader = _build_recorder()
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

    assert any(data_point.value == 5 for data_point in _metric_values(reader, "rag_token_usage"))
    assert _metric_values(reader, "rag_token_write_failure") == []


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
async def test_read_daily_usage_maps_redis_fields_and_window() -> None:
    recorder, redis, _ = _build_recorder()
    today = datetime(2026, 9, 8, 13, 30, tzinfo=ZoneInfo("Asia/Shanghai")).date()
    day_one = today - timedelta(days=2)
    day_two = today - timedelta(days=1)
    for day, values in (
        (
            day_one,
            {
                "inputTokens": 10,
                "embeddingTokens": 20,
                "evaluationTokens": 999,
                "unknownTokens": 999,
            },
        ),
        (day_two, {"answerGenerationTokens": 30}),
    ):
        redis.daily_hash_values[f"rag:token:v3:daily:{day:%Y:%m:%Y-%m-%d}"] = values

    usage = await recorder.read_daily_usage(
        days=3,
        now=datetime(2026, 9, 8, 13, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert list(usage) == [day_one, day_two, today]
    assert usage[day_one] == {"input": 10, "embedding": 20}
    assert usage[day_two] == {"answer_generation": 30}
    assert usage[today] == {}
    assert redis.hgetall_calls == 3


@pytest.mark.asyncio
async def test_read_user_tokens_derives_costs_from_configured_prices() -> None:
    redis = FakeRedis()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = TokenUsageRecorder(
        redis_client=redis,
        meter=provider.get_meter("tests"),
        embedding_price=Decimal("0.0005"),
        chat_input_price=Decimal("0.001"),
        chat_output_price=Decimal("0.002"),
        reranker_price=Decimal("0.0005"),
    )
    redis.hash_values = {
        "embeddingTokens": "125",
        "inputTokens": "890",
        "answerGenerationTokens": "8",
        "intentTokens": "5",
        "estimatedCostCny": "9.9999",
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
    assert usage.embedding_cost_cny == Decimal("0.0000625")
    assert usage.input_cost_cny == Decimal("0.00089")
    assert usage.answer_generation_cost_cny == Decimal("0.000016")
    assert usage.intent_cost_cny == Decimal("0.00001")
    assert usage.hyde_cost_cny == Decimal("0")
    assert usage.reranker_cost_cny == Decimal("0")
    assert usage.faithfulness_cost_cny == Decimal("0")
    assert usage.estimated_cost_cny == Decimal("0.0009785")


@pytest.mark.asyncio
async def test_read_user_tokens_rejects_unreadable_redis_data() -> None:
    recorder, redis, _ = _build_recorder()
    redis.hash_values = {"inputTokens": "not-a-number"}
    with pytest.raises(TokenMetricsUnavailableError):
        await recorder.read_user_tokens(7)

    redis.fail = True
    with pytest.raises(TokenMetricsUnavailableError):
        await recorder.read_user_tokens(7)
    assert redis.hgetall_calls == 3
