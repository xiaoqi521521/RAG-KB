from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families

from app.core.context import CurrentUser, current_user_var
from app.services.token_metrics import TokenUsageRecorder


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.values: dict[str, str] = {}

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> list[object]:
        name, token_field, raw_tokens, cost_field, raw_cost = keys_and_args
        tokens = int(raw_tokens)
        cost = Decimal(str(raw_cost))
        self.calls.append((str(name), str(token_field), tokens))
        self.values[str(token_field)] = str(int(self.values.get(str(token_field), "0")) + tokens)
        self.values[str(cost_field)] = str(Decimal(self.values.get(str(cost_field), "0")) + cost)
        return [self.values[str(token_field)], self.values[str(cost_field)]]

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        self.calls.append((name, key, amount))
        return amount

    async def hgetall(self, name: str) -> dict[str, str]:
        return {}


@pytest.mark.asyncio
async def test_record_chat_usage_separates_input_and_answer_output() -> None:
    redis = FakeRedis()
    registry = CollectorRegistry()
    recorder = TokenUsageRecorder(redis_client=redis, registry=registry)
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
    assert Decimal(redis.values["estimatedCostCny"]) == Decimal("0")
    families = {
        family.name: family
        for family in text_string_to_metric_families(recorder.render_metrics())
    }
    samples = families["rag_token_usage"].samples
    values = {
        (sample.labels["model"], sample.labels["token_type"], sample.labels["kb_id"]): sample.value
        for sample in samples
        if sample.name == "rag_token_usage_total"
    }
    assert values == {
        ("deepseek-v4-flash", "input", "2"): 120.0,
        ("deepseek-v4-flash", "answer_generation", "2"): 8.0,
    }


def test_record_chat_usage_does_not_estimate_missing_provider_usage() -> None:
    recorder = TokenUsageRecorder(redis_client=FakeRedis(), registry=CollectorRegistry())

    assert recorder.extract_usage(SimpleNamespace()) == (None, None)
