from __future__ import annotations

import logging

import pytest
from langchain_core.messages import AIMessage
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.core.context import CurrentUser, current_user_var
from app.services.token_metrics import (
    TokenMetrics,
    extract_generation_tokens,
    record_generation_usage,
)


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.fail = False

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        self.calls.append((name, key, amount))
        if self.fail:
            raise RuntimeError("redis unavailable")
        return amount


def _build_metrics() -> tuple[TokenMetrics, FakeRedis, InMemoryMetricReader, MeterProvider]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    redis_client = FakeRedis()
    metrics = TokenMetrics(
        redis_client=redis_client,
        meter=provider.get_meter("tests.token-metrics"),
    )
    return metrics, redis_client, reader, provider


def _metric_value(
    reader: InMemoryMetricReader,
    name: str,
    attributes: dict[str, str],
) -> int:
    data = reader.get_metrics_data()
    assert data is not None
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    if dict(point.attributes) == attributes:
                        return int(point.value)
    raise AssertionError(f"metric not found: {name} {attributes}")


@pytest.mark.asyncio
async def test_record_embedding_tokens_writes_log_counter_and_user_redis(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics, redis_client, reader, provider = _build_metrics()
    token = current_user_var.set(CurrentUser(user_id=7, department_id="eng", role="ADMIN"))
    try:
        with caplog.at_level(logging.INFO, logger="app.services.token_metrics"):
            await metrics.record_embedding_tokens(tokens=120)
    finally:
        current_user_var.reset(token)
        provider.shutdown()

    assert "record_embedding_tokens=120" in caplog.text
    assert redis_client.calls == [("rag:token-stats:7", "embeddingTokens", 120)]
    assert _metric_value(reader, "rag.tokens.embedding", {"source": "provider"}) == 120


@pytest.mark.asyncio
async def test_record_context_tokens_uses_v4_local_tiktoken_attributes() -> None:
    metrics, redis_client, reader, provider = _build_metrics()
    token = current_user_var.set(CurrentUser(user_id=8, department_id="eng", role="ADMIN"))
    try:
        await metrics.record_context_tokens(tokens=42)
    finally:
        current_user_var.reset(token)
        provider.shutdown()

    assert redis_client.calls == [("rag:token-stats:8", "contextTokens", 42)]
    assert (
        _metric_value(
            reader,
            "rag.tokens.context",
            {"pipeline": "v4", "source": "local_tiktoken"},
        )
        == 42
    )


@pytest.mark.asyncio
async def test_record_generation_tokens_without_user_only_updates_counter() -> None:
    metrics, redis_client, reader, provider = _build_metrics()
    try:
        await metrics.record_generation_tokens(tokens=88)
    finally:
        provider.shutdown()

    assert redis_client.calls == []
    assert _metric_value(reader, "rag.tokens.generation", {"source": "provider"}) == 88


@pytest.mark.asyncio
async def test_redis_failure_does_not_break_metric_recording(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics, redis_client, reader, provider = _build_metrics()
    redis_client.fail = True
    token = current_user_var.set(CurrentUser(user_id=9, department_id="eng", role="ADMIN"))
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.token_metrics"):
            await metrics.record_context_tokens(tokens=5)
    finally:
        current_user_var.reset(token)
        provider.shutdown()

    assert "Redis token metric write failed" in caplog.text
    assert (
        _metric_value(
            reader,
            "rag.tokens.context",
            {"pipeline": "v4", "source": "local_tiktoken"},
        )
        == 5
    )


@pytest.mark.asyncio
async def test_non_positive_tokens_do_not_increment_counter_or_redis() -> None:
    metrics, redis_client, reader, provider = _build_metrics()
    token = current_user_var.set(CurrentUser(user_id=10, department_id="eng", role="ADMIN"))
    try:
        await metrics.record_context_tokens(tokens=0)
        await metrics.record_context_tokens(tokens=-1)
        assert reader.get_metrics_data() is None
    finally:
        current_user_var.reset(token)
        provider.shutdown()

    assert redis_client.calls == []


def test_extract_generation_tokens_prefers_langchain_usage_metadata() -> None:
    response = AIMessage(
        content="answer",
        usage_metadata={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
    )

    assert extract_generation_tokens(response) == 8


def test_extract_generation_tokens_falls_back_to_response_metadata() -> None:
    response = AIMessage(
        content="answer",
        response_metadata={"token_usage": {"completion_tokens": 9}},
    )

    assert extract_generation_tokens(response) == 9


def test_extract_generation_tokens_returns_none_when_usage_is_unavailable() -> None:
    assert extract_generation_tokens(AIMessage(content="answer")) is None


@pytest.mark.asyncio
async def test_record_generation_usage_logs_when_provider_usage_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeRecorder:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def record_generation_tokens(
            self,
            *,
            tokens: int,
            source: str = "provider",
        ) -> None:
            self.calls.append(tokens)

    recorder = FakeRecorder()

    with caplog.at_level(logging.INFO, logger="app.services.token_metrics"):
        await record_generation_usage(
            recorder=recorder,
            response=AIMessage(content="answer"),
            pipeline="v4",
        )

    assert recorder.calls == []
    assert "generation_token_usage_unavailable=true pipeline=v4" in caplog.text
