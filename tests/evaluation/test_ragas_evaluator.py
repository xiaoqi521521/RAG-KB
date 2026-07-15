from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, NoReturn

import httpx
import openai
import pytest
from instructor.core.exceptions import InstructorRetryException
from ragas.embeddings.base import BaseRagasEmbedding
from ragas.llms.base import InstructorBaseRagasLLM

from app.evaluation.ragas_evaluator import (
    RagasErrorType,
    RagasEvaluationSample,
    RagasEvaluator,
    RagasMetricError,
    RagasMetricName,
    RagasMetrics,
    build_ragas_metrics,
)
from app.integrations.openai_embeddings import OpenAICompatibleEmbeddings


class FakeMetric:
    def __init__(self, score: float) -> None:
        self.score = score
        self.calls: list[dict[str, Any]] = []

    async def ascore(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(value=self.score)


@pytest.mark.asyncio
async def test_evaluate_maps_inputs_and_returns_four_independent_scores() -> None:
    faithfulness = FakeMetric(0.91)
    answer_relevancy = FakeMetric(0.82)
    context_recall = FakeMetric(0.73)
    context_precision = FakeMetric(0.64)
    evaluator = RagasEvaluator(
        metrics=RagasMetrics(
            faithfulness=faithfulness,
            answer_relevancy=answer_relevancy,
            context_recall=context_recall,
            context_precision=context_precision,
        )
    )

    result = await evaluator.evaluate(
        RagasEvaluationSample(
            question="报销时限？",
            actual_answer="应在三十天内提交。",
            expected_answer="费用发生后三十天内提交。",
            reference_contexts=["制度第一条", "制度第二条"],
        )
    )

    assert result.faithfulness == 0.91
    assert result.answer_relevancy == 0.82
    assert result.context_recall == 0.73
    assert result.context_precision == 0.64
    assert result.errors == ()
    assert faithfulness.calls == [
        {
            "user_input": "报销时限？",
            "response": "应在三十天内提交。",
            "retrieved_contexts": ["制度第一条", "制度第二条"],
        }
    ]
    assert answer_relevancy.calls == [
        {"user_input": "报销时限？", "response": "应在三十天内提交。"}
    ]
    expected_context_call = {
        "user_input": "报销时限？",
        "reference": "费用发生后三十天内提交。",
        "retrieved_contexts": ["制度第一条", "制度第二条"],
    }
    assert context_recall.calls == [expected_context_call]
    assert context_precision.calls == [expected_context_call]


@pytest.mark.asyncio
async def test_evaluate_runs_all_four_metrics_concurrently() -> None:
    started = 0
    all_started = asyncio.Event()

    class ConcurrentMetric(FakeMetric):
        async def ascore(self, **kwargs: Any) -> SimpleNamespace:
            nonlocal started
            started += 1
            if started == 4:
                all_started.set()
            await all_started.wait()
            return await super().ascore(**kwargs)

    metric = ConcurrentMetric(0.75)
    evaluator = RagasEvaluator(
        metrics=RagasMetrics(
            faithfulness=metric,
            answer_relevancy=metric,
            context_recall=metric,
            context_precision=metric,
        )
    )

    result = await asyncio.wait_for(
        evaluator.evaluate(
            RagasEvaluationSample(
                question="问题",
                actual_answer="回答",
                expected_answer="期望回答",
                reference_contexts=["参考内容"],
            )
        ),
        timeout=0.1,
    )

    assert started == 4
    assert result.faithfulness == 0.75
    assert result.answer_relevancy == 0.75
    assert result.context_recall == 0.75
    assert result.context_precision == 0.75


@pytest.mark.asyncio
async def test_evaluate_isolates_one_metric_failure_without_sensitive_error_text() -> None:
    class ParseFailingMetric(FakeMetric):
        async def ascore(self, **kwargs: Any) -> SimpleNamespace:
            self.calls.append(kwargs)
            raise InstructorRetryException(
                "供应商响应包含问题与回答正文",
                n_attempts=1,
                total_usage=0,
                create_kwargs={"messages": ["问题与回答正文"]},
            )

    context_recall = ParseFailingMetric(0.0)
    evaluator = RagasEvaluator(
        metrics=RagasMetrics(
            faithfulness=FakeMetric(0.9),
            answer_relevancy=FakeMetric(0.8),
            context_recall=context_recall,
            context_precision=FakeMetric(0.7),
        )
    )

    result = await evaluator.evaluate(
        RagasEvaluationSample(
            question="问题",
            actual_answer="回答",
            expected_answer="期望回答",
            reference_contexts=["参考内容"],
        )
    )

    assert result.faithfulness == 0.9
    assert result.answer_relevancy == 0.8
    assert result.context_recall is None
    assert result.context_precision == 0.7
    assert result.errors == (
        RagasMetricError(
            metric=RagasMetricName.CONTEXT_RECALL,
            error_type=RagasErrorType.PARSE_ERROR,
        ),
    )
    assert len(context_recall.calls) == 1
    assert "供应商响应" not in repr(result)


@pytest.mark.asyncio
async def test_evaluate_retries_one_metric_once_after_timeout() -> None:
    class TimeoutOnceMetric(FakeMetric):
        async def ascore(self, **kwargs: Any) -> SimpleNamespace:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                await asyncio.sleep(1)
            return SimpleNamespace(value=self.score)

    faithfulness = TimeoutOnceMetric(0.88)
    evaluator = RagasEvaluator(
        metrics=RagasMetrics(
            faithfulness=faithfulness,
            answer_relevancy=FakeMetric(0.8),
            context_recall=FakeMetric(0.7),
            context_precision=FakeMetric(0.6),
        ),
        timeout_seconds=0.01,
    )

    result = await evaluator.evaluate(
        RagasEvaluationSample(
            question="问题",
            actual_answer="回答",
            expected_answer="期望回答",
            reference_contexts=["参考内容"],
        )
    )

    assert result.faithfulness == 0.88
    assert result.errors == ()
    assert len(faithfulness.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error_type"),
    [
        (
            openai.RateLimitError(
                "limited",
                response=httpx.Response(
                    429,
                    request=httpx.Request("POST", "https://provider.test/v1/chat"),
                ),
                body=None,
            ),
            RagasErrorType.RATE_LIMIT,
        ),
        (
            httpx.ConnectError(
                "connection failed",
                request=httpx.Request("POST", "https://provider.test/v1/chat"),
            ),
            RagasErrorType.TEMPORARY_PROVIDER,
        ),
    ],
)
async def test_evaluate_retries_only_temporary_provider_failures(
    error: Exception,
    expected_error_type: RagasErrorType,
) -> None:
    class FailingMetric(FakeMetric):
        async def ascore(self, **kwargs: Any) -> NoReturn:
            self.calls.append(kwargs)
            raise error

    faithfulness = FailingMetric(0.0)
    evaluator = RagasEvaluator(
        metrics=RagasMetrics(
            faithfulness=faithfulness,
            answer_relevancy=FakeMetric(0.8),
            context_recall=FakeMetric(0.7),
            context_precision=FakeMetric(0.6),
        )
    )

    result = await evaluator.evaluate(
        RagasEvaluationSample(
            question="问题",
            actual_answer="回答",
            expected_answer="期望回答",
            reference_contexts=["参考内容"],
        )
    )

    assert result.faithfulness is None
    assert result.answer_relevancy == 0.8
    assert result.errors == (
        RagasMetricError(
            metric=RagasMetricName.FAITHFULNESS,
            error_type=expected_error_type,
        ),
    )
    assert len(faithfulness.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_score", [float("nan"), float("inf"), float("-inf"), -0.1, 1.1])
async def test_evaluate_rejects_invalid_scores_without_retry(invalid_score: float) -> None:
    faithfulness = FakeMetric(invalid_score)
    evaluator = RagasEvaluator(
        metrics=RagasMetrics(
            faithfulness=faithfulness,
            answer_relevancy=FakeMetric(0.8),
            context_recall=FakeMetric(0.7),
            context_precision=FakeMetric(0.6),
        )
    )

    result = await evaluator.evaluate(
        RagasEvaluationSample(
            question="问题",
            actual_answer="回答",
            expected_answer="期望回答",
            reference_contexts=["参考内容"],
        )
    )

    assert result.faithfulness is None
    assert result.answer_relevancy == 0.8
    assert result.errors == (
        RagasMetricError(
            metric=RagasMetricName.FAITHFULNESS,
            error_type=RagasErrorType.INVALID_SCORE,
        ),
    )
    assert len(faithfulness.calls) == 1


@pytest.mark.asyncio
async def test_build_ragas_metrics_executes_locked_runtime_with_external_fakes() -> None:
    class FakeInstructorLLM(InstructorBaseRagasLLM):
        def generate(self, prompt: str, response_model: type[Any]) -> Any:
            raise AssertionError("异步评估不应调用同步模型接口")

        async def agenerate(self, prompt: str, response_model: type[Any]) -> Any:
            outputs: dict[str, dict[str, Any]] = {
                "StatementGeneratorOutput": {"statements": ["应在三十天内提交。"]},
                "NLIStatementOutput": {
                    "statements": [
                        {
                            "statement": "应在三十天内提交。",
                            "reason": "参考内容支持该陈述",
                            "verdict": 1,
                        }
                    ]
                },
                "AnswerRelevanceOutput": {"question": "报销时限？", "noncommittal": 0},
                "ContextRecallOutput": {
                    "classifications": [
                        {
                            "statement": "费用发生后三十天内提交。",
                            "reason": "参考内容包含该要求",
                            "attributed": 1,
                        }
                    ]
                },
                "ContextPrecisionOutput": {"reason": "参考内容可用于回答", "verdict": 1},
            }
            return response_model.model_validate(outputs[response_model.__name__])

    class FakeRagasEmbeddings(BaseRagasEmbedding):
        def embed_text(self, text: str, **kwargs: Any) -> list[float]:
            return [1.0, 0.0]

        async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
            return [1.0, 0.0]

    evaluator = RagasEvaluator(
        metrics=build_ragas_metrics(
            llm=FakeInstructorLLM(),
            embeddings=FakeRagasEmbeddings(),
        )
    )

    result = await evaluator.evaluate(
        RagasEvaluationSample(
            question="报销时限？",
            actual_answer="应在三十天内提交。",
            expected_answer="费用发生后三十天内提交。",
            reference_contexts=["费用发生后三十天内提交。"],
        )
    )

    assert result.faithfulness == 1.0
    assert result.answer_relevancy == 1.0
    assert result.context_recall == 1.0
    assert result.context_precision == pytest.approx(1.0)
    assert result.errors == ()


@pytest.mark.asyncio
async def test_from_clients_reuses_existing_chat_and_embedding_clients() -> None:
    class FakeEmbeddingApi:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> SimpleNamespace:
            self.calls.append(kwargs)
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=index, embedding=[1.0, float(index)])
                    for index, _ in enumerate(kwargs["input"])
                ],
                usage=SimpleNamespace(total_tokens=4),
            )

    embedding_api = FakeEmbeddingApi()
    embeddings = OpenAICompatibleEmbeddings(
        embeddings_api=embedding_api,
        model="text-embedding-v3",
    )
    root_client = openai.AsyncOpenAI(
        api_key="test-key",
        base_url="https://provider.test/v1",
        max_retries=2,
    )
    chat_model = SimpleNamespace(
        model_name="deepseek-v4-flash",
        root_async_client=root_client,
    )

    evaluator = RagasEvaluator.from_clients(
        chat_model=chat_model,
        embeddings=embeddings,
    )
    ragas_embeddings = evaluator.metrics.answer_relevancy.embeddings
    vectors = await ragas_embeddings.aembed_texts(["问题", "生成问题"])

    assert vectors == [[1.0, 0.0], [1.0, 1.0]]
    assert embedding_api.calls == [
        {"input": ["问题", "生成问题"], "model": "text-embedding-v3"}
    ]
    assert evaluator.timeout_seconds == 30.0
    await root_client.close()


@pytest.mark.asyncio
async def test_evaluate_rejects_only_metrics_with_invalid_inputs_without_calling_them() -> None:
    faithfulness = FakeMetric(0.9)
    answer_relevancy = FakeMetric(0.8)
    context_recall = FakeMetric(0.7)
    context_precision = FakeMetric(0.6)
    evaluator = RagasEvaluator(
        metrics=RagasMetrics(
            faithfulness=faithfulness,
            answer_relevancy=answer_relevancy,
            context_recall=context_recall,
            context_precision=context_precision,
        )
    )

    result = await evaluator.evaluate(
        RagasEvaluationSample(
            question="问题",
            actual_answer="回答",
            expected_answer="   ",
            reference_contexts=["参考内容"],
        )
    )

    assert result.faithfulness == 0.9
    assert result.answer_relevancy == 0.8
    assert result.context_recall is None
    assert result.context_precision is None
    assert result.errors == (
        RagasMetricError(
            metric=RagasMetricName.CONTEXT_RECALL,
            error_type=RagasErrorType.INVALID_INPUT,
        ),
        RagasMetricError(
            metric=RagasMetricName.CONTEXT_PRECISION,
            error_type=RagasErrorType.INVALID_INPUT,
        ),
    )
    assert len(faithfulness.calls) == 1
    assert len(answer_relevancy.calls) == 1
    assert context_recall.calls == []
    assert context_precision.calls == []
