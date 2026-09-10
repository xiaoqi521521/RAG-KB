from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.core.context import CurrentUser, current_user_var
from app.services.token_metrics import TokenUsageRecorder


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.daily_calls: list[tuple[str, str, int]] = []
        self.values: dict[str, str] = {}

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> list[object]:
        name, token_field, raw_tokens = keys_and_args
        tokens = int(raw_tokens)
        self.calls.append((str(name), str(token_field), tokens))
        self.values[str(token_field)] = str(int(self.values.get(str(token_field), "0")) + tokens)
        return [self.values[str(token_field)]]

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        self.daily_calls.append((name, key, amount))
        return amount

    async def hgetall(self, name: str) -> dict[str, str]:
        return {}


def _build_metrics() -> tuple[MeterProvider, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    return MeterProvider(metric_readers=[reader]), reader


def _usage_data_points(reader: InMemoryMetricReader) -> list[object]:
    metrics_data = reader.get_metrics_data()
    return [
        data_point
        for resource in metrics_data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "rag_token_usage"
        for data_point in metric.data.data_points
    ]


@pytest.mark.asyncio
async def test_record_chat_usage_separates_input_and_answer_output() -> None:
    redis = FakeRedis()
    provider, reader = _build_metrics()
    recorder = TokenUsageRecorder(redis_client=redis, meter=provider.get_meter("tests"))
    user_token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="USER"))
    try:
        await recorder.record_chat_usage(
            response=SimpleNamespace(
                usage_metadata={"input_tokens": 120, "output_tokens": 8},
            ),
            model="deepseek-v4-flash",
            output_type="answer_generation",
            kb_id="2",
        )
    finally:
        current_user_var.reset(user_token)

    assert redis.calls == [
        ("rag:token:v3:stats:7", "inputTokens", 120),
        ("rag:token:v3:stats:7", "answerGenerationTokens", 8),
    ]
    assert redis.daily_calls == [
        (recorder._daily_usage_key(), "inputTokens", 120),
        (recorder._daily_usage_key(), "answerGenerationTokens", 8),
    ]
    assert "estimatedCostCny" not in redis.values
    values = {
        (
            data_point.attributes["model"],
            data_point.attributes["token_type"],
            data_point.attributes["kb_id"],
        ): data_point.value
        for data_point in _usage_data_points(reader)
    }
    assert values == {
        ("deepseek-v4-flash", "input", "2"): 120.0,
        ("deepseek-v4-flash", "answer_generation", "2"): 8.0,
    }


@pytest.mark.asyncio
async def test_record_chat_usage_separates_input_and_intent_output() -> None:
    redis = FakeRedis()
    provider, _ = _build_metrics()
    recorder = TokenUsageRecorder(redis_client=redis, meter=provider.get_meter("tests"))
    user_token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="USER"))
    try:
        await recorder.record_chat_usage(
            response=SimpleNamespace(
                usage_metadata={"input_tokens": 60, "output_tokens": 4},
            ),
            model="deepseek-v4-flash",
            output_type="intent",
            kb_id="multi",
        )
    finally:
        current_user_var.reset(user_token)

    assert redis.calls == [
        ("rag:token:v3:stats:7", "inputTokens", 60),
        ("rag:token:v3:stats:7", "intentTokens", 4),
    ]
    assert redis.daily_calls == [
        (recorder._daily_usage_key(), "inputTokens", 60),
        (recorder._daily_usage_key(), "intentTokens", 4),
    ]


def test_record_chat_usage_does_not_estimate_missing_provider_usage() -> None:
    provider, _ = _build_metrics()
    recorder = TokenUsageRecorder(redis_client=FakeRedis(), meter=provider.get_meter("tests"))

    assert recorder.extract_usage(SimpleNamespace()) == (None, None)
