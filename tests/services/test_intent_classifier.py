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
        ("UNCERTAIN", ChatIntent.UNCERTAIN),
    ],
)
async def test_classify_accepts_only_supported_intents(value: str, expected: ChatIntent) -> None:
    model = FakeModel([SimpleNamespace(content=f'{{"intent":"{value}"}}')])
    metrics = FakeTokenMetrics()

    result = await IntentClassifier(
        model,
        token_metrics=metrics,
        model_name="router-model",
    ).classify("刚才问了什么？")

    assert result == expected
    assert len(model.calls) == 1
    assert len(model.calls[0]) == 2
    assert "<user_question>" in model.calls[0][1].content
    assert "刚才问了什么？" in model.calls[0][1].content


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        '{"intent":"GENERAL_CHAT","reason":"extra"}',
        '{"intent":"UNKNOWN"}',
    ],
)
async def test_classify_rejects_invalid_output_without_retry(content: str) -> None:
    model = FakeModel([SimpleNamespace(content=content)])

    with pytest.raises(HTTPException) as error:
        await IntentClassifier(model).classify("问题")

    assert error.value.status_code == 503
    assert len(model.calls) == 1


async def test_classify_retries_transient_model_failure_once() -> None:
    model = FakeModel(
        [RuntimeError("temporary"), SimpleNamespace(content='{"intent":"GENERAL_CHAT"}')]
    )

    result = await IntentClassifier(model).classify("帮我写一段介绍")

    assert result == ChatIntent.GENERAL_CHAT
    assert len(model.calls) == 2


async def test_classify_checks_budget_before_the_first_model_call() -> None:
    model = FakeModel([SimpleNamespace(content='{"intent":"GENERAL_CHAT"}')])
    budget_gate = FakeBudgetGate()

    result = await IntentClassifier(model, budget_gate=budget_gate).classify("帮我写一段介绍")

    assert result == ChatIntent.GENERAL_CHAT
    assert budget_gate.ensure_calls == 1


async def test_classify_returns_503_after_retry_failure() -> None:
    model = FakeModel([TimeoutError(), TimeoutError()])

    with pytest.raises(HTTPException) as error:
        await IntentClassifier(model).classify("问题")

    assert error.value.status_code == 503
    assert len(model.calls) == 2
