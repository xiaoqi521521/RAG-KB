from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.faithfulness_evaluator import (
    FaithfulnessEvaluator,
    FaithfulnessStatus,
)


class FakeChatModel:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0

    async def ainvoke(self, messages: list[object]) -> object:
        self.calls += 1
        return self.response


class FakeTokenMetrics:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record_generation_tokens(self, *, tokens: int, source: str = "provider") -> None:
        self.calls.append({"tokens": tokens, "source": source})


@pytest.mark.asyncio
async def test_evaluate_returns_normalized_faithful_result_and_records_extra_tokens() -> None:
    chat_model = FakeChatModel(
        SimpleNamespace(
            content="忠实性分数：8\n是否忠实：是",
            usage_metadata={"output_tokens": 12},
        )
    )
    token_metrics = FakeTokenMetrics()
    evaluator = FaithfulnessEvaluator(
        chat_model=chat_model,
        token_metrics=token_metrics,
        sampling_rate=1,
        timeout_seconds=5,
    )

    result = await evaluator.evaluate(
        question="代码提交前需要做什么？",
        answer="代码提交前必须通过本地测试。",
        context="[参考1]\n代码提交前必须通过本地测试。",
    )

    assert result.status is FaithfulnessStatus.FAITHFUL
    assert result.score == 0.8
    assert result.sampled is True
    assert chat_model.calls == 1
    assert token_metrics.calls == [{"tokens": 12, "source": "faithfulness_evaluation"}]


@pytest.mark.asyncio
async def test_evaluate_returns_unfaithful_result_with_reason() -> None:
    evaluator = FaithfulnessEvaluator(
        chat_model=FakeChatModel(
            SimpleNamespace(content="忠实性分数: 3\n是否忠实: 否\n理由: 回答包含参考内容没有的要求")
        ),
        token_metrics=FakeTokenMetrics(),
        sampling_rate=1,
        timeout_seconds=5,
    )

    result = await evaluator.evaluate(question="问题", answer="回答", context="参考内容")

    assert result.status is FaithfulnessStatus.UNFAITHFUL
    assert result.score == 0.3
    assert result.reason == "回答包含参考内容没有的要求"
    assert result.sampled is True


@pytest.mark.asyncio
async def test_evaluate_skips_model_call_when_sampling_is_disabled() -> None:
    chat_model = FakeChatModel(SimpleNamespace(content="不应调用"))
    evaluator = FaithfulnessEvaluator(
        chat_model=chat_model,
        token_metrics=FakeTokenMetrics(),
        sampling_rate=0,
        timeout_seconds=5,
    )

    result = await evaluator.evaluate(question="问题", answer="回答", context="参考内容")

    assert result.status is FaithfulnessStatus.SKIPPED
    assert result.score is None
    assert result.sampled is False
    assert chat_model.calls == 0


@pytest.mark.asyncio
async def test_evaluate_skips_model_call_when_partial_sampling_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.faithfulness_evaluator.random.random", lambda: 0.8)
    chat_model = FakeChatModel(SimpleNamespace(content="不应调用"))
    evaluator = FaithfulnessEvaluator(
        chat_model=chat_model,
        token_metrics=FakeTokenMetrics(),
        sampling_rate=0.2,
        timeout_seconds=5,
    )

    result = await evaluator.evaluate(question="问题", answer="回答", context="参考内容")

    assert result.status is FaithfulnessStatus.SKIPPED
    assert result.sampled is False
    assert chat_model.calls == 0


@pytest.mark.asyncio
async def test_evaluate_returns_error_for_invalid_response_format() -> None:
    evaluator = FaithfulnessEvaluator(
        chat_model=FakeChatModel(SimpleNamespace(content="我认为答案基本正确")),
        token_metrics=FakeTokenMetrics(),
        sampling_rate=1,
        timeout_seconds=5,
    )

    result = await evaluator.evaluate(question="问题", answer="回答", context="参考内容")

    assert result.status is FaithfulnessStatus.ERROR
    assert result.score is None
    assert result.reason == "invalid_response_format"


@pytest.mark.asyncio
async def test_evaluate_returns_error_without_retry_when_model_fails() -> None:
    class FailingChatModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, messages: list[object]) -> object:
            self.calls += 1
            raise RuntimeError("unavailable")

    chat_model = FailingChatModel()
    evaluator = FaithfulnessEvaluator(
        chat_model=chat_model,
        token_metrics=FakeTokenMetrics(),
        sampling_rate=1,
        timeout_seconds=5,
    )

    result = await evaluator.evaluate(question="问题", answer="回答", context="参考内容")

    assert result.status is FaithfulnessStatus.ERROR
    assert result.score is None
    assert result.reason == "RuntimeError"
    assert chat_model.calls == 1


@pytest.mark.asyncio
async def test_evaluate_returns_error_when_model_exceeds_independent_timeout() -> None:
    class SlowChatModel:
        async def ainvoke(self, messages: list[object]) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    evaluator = FaithfulnessEvaluator(
        chat_model=SlowChatModel(),
        token_metrics=FakeTokenMetrics(),
        sampling_rate=1,
        timeout_seconds=0.01,
    )

    result = await evaluator.evaluate(question="问题", answer="回答", context="参考内容")

    assert result.status is FaithfulnessStatus.ERROR
    assert result.score is None
    assert result.reason == "timeout"
