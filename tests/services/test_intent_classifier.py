from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.schemas.rag import ChatIntent
from app.services.intent_classifier import IntentClassifier


class FakeModel:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[list[object]] = []

    async def ainvoke(self, messages: list[object]) -> object:
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTokenMetrics:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record_chat_usage(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class FakeBudgetGate:
    def __init__(self) -> None:
        self.ensure_calls = 0

    async def ensure_available(self) -> None:
        self.ensure_calls += 1

    @asynccontextmanager
    async def request_scope(self):
        yield


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("KNOWLEDGE_BASE_QUERY", ChatIntent.KNOWLEDGE_BASE_QUERY),
        ("SESSION_META", ChatIntent.SESSION_META),
        ("GENERAL_CHAT", ChatIntent.GENERAL_CHAT),
        ("MIXED", ChatIntent.MIXED),
        ("UNKNOWN", ChatIntent.UNKNOWN),
    ],
)
async def test_classify_accepts_only_supported_intents(value: str, expected: ChatIntent) -> None:
    model = FakeModel([SimpleNamespace(content=f'{{"intent":"{value}"}}')])
    metrics = FakeTokenMetrics()

    result = await IntentClassifier(
        model,
        token_metrics=metrics,
        model_name="router-model",
    ).classify_with_context("请针对企业内部聊天路由给出一个明确且完整的意图分类结果。")

    assert result.intent == expected
    assert len(model.calls) == 1
    assert len(model.calls[0]) == 2
    assert "<user_question>" in model.calls[0][1].content
    assert "请针对企业内部聊天路由给出一个明确且完整的意图分类结果。" in model.calls[0][1].content


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        '{"intent":"GENERAL_CHAT","reason":"extra"}',
        '{"intent":"NOT_A_SUPPORTED_INTENT"}',
    ],
)
async def test_classify_rejects_invalid_output_without_retry(content: str) -> None:
    model = FakeModel([SimpleNamespace(content=content)])

    with pytest.raises(HTTPException) as error:
        await IntentClassifier(model).classify_with_context("问题")

    assert error.value.status_code == 503
    assert len(model.calls) == 1


async def test_classify_retries_transient_model_failure_once() -> None:
    model = FakeModel(
        [RuntimeError("temporary"), SimpleNamespace(content='{"intent":"GENERAL_CHAT"}')]
    )

    result = await IntentClassifier(model).classify_with_context("帮我写一段介绍")

    assert result.intent == ChatIntent.GENERAL_CHAT
    assert len(model.calls) == 2


async def test_classify_checks_budget_before_the_first_model_call() -> None:
    model = FakeModel([SimpleNamespace(content='{"intent":"GENERAL_CHAT"}')])
    budget_gate = FakeBudgetGate()

    result = await IntentClassifier(model, budget_gate=budget_gate).classify_with_context("帮我写一段介绍")

    assert result.intent == ChatIntent.GENERAL_CHAT
    assert budget_gate.ensure_calls == 1


async def test_classify_returns_503_after_retry_failure() -> None:
    model = FakeModel([TimeoutError(), TimeoutError()])

    with pytest.raises(HTTPException) as error:
        await IntentClassifier(model).classify_with_context("问题")

    assert error.value.status_code == 503
    assert len(model.calls) == 2


async def test_follow_up_rewrite_model_expands_reference_question() -> None:
    model = FakeModel([
        SimpleNamespace(content='{"intent":"KNOWLEDGE_BASE_QUERY"}'),
        SimpleNamespace(content='{"needs_rewrite":true,"question":"年假申请需要准备哪些材料？"}'),
    ])
    classifier = IntentClassifier(model)
    decision = await classifier.classify_with_context(
        "这个需要准备什么？",
        history=[SimpleNamespace(role="USER", content="年假怎么申请？")],
    )
    assert decision.intent == ChatIntent.KNOWLEDGE_BASE_QUERY
    assert decision.rewritten_question == "年假申请需要准备哪些材料？"
    assert len(model.calls) == 2


async def test_follow_up_rewrite_model_keeps_original_when_no_rewrite_needed() -> None:
    model = FakeModel([
        SimpleNamespace(content='{"intent":"KNOWLEDGE_BASE_QUERY"}'),
        SimpleNamespace(content='{"needs_rewrite":false,"question":"模型错误复述"}'),
    ])
    decision = await IntentClassifier(model).classify_with_context(
        "年假怎么申请？",
        history=[SimpleNamespace(role="USER", content="上一问")],
    )
    assert decision.rewritten_question == "年假怎么申请？"
    assert len(model.calls) == 2


async def test_rewrite_failure_is_reported_as_rewrite_service_unavailable() -> None:
    model = FakeModel([
        SimpleNamespace(content='{"intent":"KNOWLEDGE_BASE_QUERY"}'),
        SimpleNamespace(content="not-json"),
    ])

    with pytest.raises(HTTPException) as error:
        await IntentClassifier(model).classify_with_context(
            "这个需要准备什么？",
            history=[SimpleNamespace(role="USER", content="年假怎么申请？")],
        )

    assert error.value.status_code == 503
    assert error.value.detail == "问题改写服务暂不可用"
    assert len(model.calls) == 2


async def test_first_turn_never_enters_follow_up_rewrite() -> None:
    model = FakeModel([SimpleNamespace(content='{"intent":"KNOWLEDGE_BASE_QUERY"}')])

    decision = await IntentClassifier(model).classify_with_context("这个需要准备什么？")

    assert decision.rewritten_question is None
    assert len(model.calls) == 1


async def test_classifier_context_keeps_latest_ten_rounds() -> None:
    model = FakeModel([SimpleNamespace(content='{"intent":"GENERAL_CHAT"}')])
    history = [
        SimpleNamespace(role="USER", content=f"历史-{index}")
        for index in range(22)
    ]

    await IntentClassifier(model).classify_with_context("当前问题", history=history)

    prompt = model.calls[0][1].content
    history_lines = prompt.split("<conversation_history>\n", 1)[1].split("\n</conversation_history>", 1)[0].splitlines()
    assert "USER: 历史-0" not in history_lines
    assert "USER: 历史-1" not in history_lines
    assert "USER: 历史-2" in history_lines
    assert "USER: 历史-21" in history_lines
