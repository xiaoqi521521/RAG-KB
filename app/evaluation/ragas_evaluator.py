from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
import openai
from instructor.core.exceptions import InstructorRetryException
from ragas.embeddings.base import BaseRagasEmbedding
from ragas.llms import llm_factory
from ragas.llms.base import InstructorBaseRagasLLM
from ragas.metrics.collections import (
    AnswerRelevancy,
    ContextPrecisionWithReference,
    ContextRecall,
    Faithfulness,
)

from app.integrations.openai_embeddings import OpenAICompatibleEmbeddings


class FaithfulnessMetric(Protocol):
    """Faithfulness 的公开异步评分接口。"""

    async def ascore(
        self,
        user_input: str,
        response: str,
        retrieved_contexts: list[str],
    ) -> object: ...


class AnswerRelevancyMetric(Protocol):
    """Answer Relevancy 的公开异步评分接口。"""

    async def ascore(self, user_input: str, response: str) -> object: ...


class ContextRecallMetric(Protocol):
    """Context Recall 的公开异步评分接口。"""

    async def ascore(
        self,
        user_input: str,
        retrieved_contexts: list[str],
        reference: str,
    ) -> object: ...


class ContextPrecisionMetric(Protocol):
    """Context Precision 的公开异步评分接口。"""

    async def ascore(
        self,
        user_input: str,
        reference: str,
        retrieved_contexts: list[str],
    ) -> object: ...


class RagasMetricName(StrEnum):
    """正式评估支持的 RAGAS 指标名。"""

    FAITHFULNESS = "faithfulness"
    ANSWER_RELEVANCY = "answer_relevancy"
    CONTEXT_RECALL = "context_recall"
    CONTEXT_PRECISION = "context_precision"


class RagasErrorType(StrEnum):
    """不包含供应商正文的稳定错误分类。"""

    PARSE_ERROR = "parse_error"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TEMPORARY_PROVIDER = "temporary_provider"
    PROVIDER_ERROR = "provider_error"
    INVALID_SCORE = "invalid_score"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True)
class RagasMetrics:
    """正式评估使用的四项 RAGAS 指标。"""

    faithfulness: FaithfulnessMetric
    answer_relevancy: AnswerRelevancyMetric
    context_recall: ContextRecallMetric
    context_precision: ContextPrecisionMetric


def build_ragas_metrics(
    *,
    llm: InstructorBaseRagasLLM,
    embeddings: BaseRagasEmbedding,
) -> RagasMetrics:
    """使用锁定的 RAGAS 类型构建四项现代异步指标。"""
    return RagasMetrics(
        faithfulness=Faithfulness(llm=llm),
        answer_relevancy=AnswerRelevancy(llm=llm, embeddings=embeddings),
        context_recall=ContextRecall(llm=llm),
        context_precision=ContextPrecisionWithReference(llm=llm),
    )


class _OpenAICompatibleRagasEmbeddings(BaseRagasEmbedding):
    """将项目现有异步 Embedding 客户端接入 RAGAS 现代接口。"""

    def __init__(self, embeddings: OpenAICompatibleEmbeddings) -> None:
        super().__init__()
        self.embeddings = embeddings

    def embed_text(self, text: str, **kwargs: Any) -> list[float]:
        """为 RAGAS 同步入口复用同一个异步客户端。"""
        return self._run_async_in_current_loop(self.aembed_text(text, **kwargs))

    async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
        """异步生成单条文本向量。"""
        vectors = await self.aembed_texts([text], **kwargs)
        return vectors[0]

    def embed_texts(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        """为 RAGAS 同步批量入口复用同一个异步客户端。"""
        return self._run_async_in_current_loop(self.aembed_texts(texts, **kwargs))

    async def aembed_texts(
        self,
        texts: list[str],
        **kwargs: Any,
    ) -> list[list[float]]:
        """异步批量生成向量，并保持项目客户端的输入顺序语义。"""
        vectors, _ = await self.embeddings.aembed_documents_with_usage(texts)
        return vectors


@dataclass(frozen=True)
class RagasEvaluationSample:
    """一次生成质量评估所需的标准化输入。"""

    question: str
    actual_answer: str
    expected_answer: str
    reference_contexts: list[str]


@dataclass(frozen=True)
class RagasMetricError:
    """单项指标的低基数失败信息。"""

    metric: RagasMetricName
    error_type: RagasErrorType


@dataclass(frozen=True)
class _MetricOutcome:
    """单项评分的内部成功或失败结果。"""

    score: float | None
    error: RagasMetricError | None


@dataclass(frozen=True)
class RagasEvaluationResult:
    """四项独立可空分数及其失败分类。"""

    faithfulness: float | None
    answer_relevancy: float | None
    context_recall: float | None
    context_precision: float | None
    errors: tuple[RagasMetricError, ...]


class RagasEvaluator:
    """隔离 RAGAS 输入映射和单题指标执行。"""

    def __init__(
        self,
        *,
        metrics: RagasMetrics,
        timeout_seconds: float = 30.0,
    ) -> None:
        """初始化四项指标依赖。"""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.metrics = metrics
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_clients(
        cls,
        *,
        chat_model: Any,
        embeddings: OpenAICompatibleEmbeddings,
    ) -> RagasEvaluator:
        """复用现有回答模型和 Embedding 客户端构建正式评估适配器。"""
        model_name = getattr(chat_model, "model_name", None)
        root_async_client = getattr(chat_model, "root_async_client", None)
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("chat_model must expose a nonblank model_name")
        if root_async_client is None:
            raise ValueError("chat_model must expose root_async_client")

        # 禁用 SDK 内部重试，确保重试次数只由本适配器控制。
        evaluation_client = root_async_client.with_options(max_retries=0)
        llm = llm_factory(model_name, client=evaluation_client, max_retries=1)
        ragas_embeddings = _OpenAICompatibleRagasEmbeddings(embeddings)
        return cls(
            metrics=build_ragas_metrics(
                llm=llm,
                embeddings=ragas_embeddings,
            )
        )

    async def evaluate(self, sample: RagasEvaluationSample) -> RagasEvaluationResult:
        """计算一个标准问题的四项生成质量分数。"""
        common_context_input = {
            "user_input": sample.question,
            "retrieved_contexts": sample.reference_contexts,
        }
        faithfulness, answer_relevancy, context_recall, context_precision = (
            await asyncio.gather(
                self._evaluate_metric(
                    RagasMetricName.FAITHFULNESS,
                    self.metrics.faithfulness,
                    {
                        **common_context_input,
                        "response": sample.actual_answer,
                    },
                ),
                self._evaluate_metric(
                    RagasMetricName.ANSWER_RELEVANCY,
                    self.metrics.answer_relevancy,
                    {
                        "user_input": sample.question,
                        "response": sample.actual_answer,
                    },
                ),
                self._evaluate_metric(
                    RagasMetricName.CONTEXT_RECALL,
                    self.metrics.context_recall,
                    {
                        **common_context_input,
                        "reference": sample.expected_answer,
                    },
                ),
                self._evaluate_metric(
                    RagasMetricName.CONTEXT_PRECISION,
                    self.metrics.context_precision,
                    {
                        **common_context_input,
                        "reference": sample.expected_answer,
                    },
                ),
            )
        )
        outcomes = (faithfulness, answer_relevancy, context_recall, context_precision)
        return RagasEvaluationResult(
            faithfulness=faithfulness.score,
            answer_relevancy=answer_relevancy.score,
            context_recall=context_recall.score,
            context_precision=context_precision.score,
            errors=tuple(outcome.error for outcome in outcomes if outcome.error is not None),
        )

    async def _evaluate_metric(
        self,
        metric_name: RagasMetricName,
        metric: Any,
        inputs: dict[str, Any],
    ) -> _MetricOutcome:
        """执行单项评分，并将异常正文收敛为稳定分类。"""
        if self._has_invalid_input(inputs):
            return self._error_outcome(metric_name, RagasErrorType.INVALID_INPUT)

        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    metric.ascore(**inputs),
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                error_type, retryable = self._classify_error(exc)
                if attempt == 0 and retryable:
                    continue
                return self._error_outcome(metric_name, error_type)
            else:
                try:
                    score = float(getattr(result, "value"))
                except (AttributeError, TypeError, ValueError):
                    return self._error_outcome(metric_name, RagasErrorType.PARSE_ERROR)
                if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                    return self._error_outcome(metric_name, RagasErrorType.INVALID_SCORE)
                return _MetricOutcome(score=score, error=None)

        raise AssertionError("unreachable metric attempt state")

    @staticmethod
    def _has_invalid_input(inputs: dict[str, Any]) -> bool:
        """按单项 RAGAS 输入形状拒绝空字符串和空参考内容。"""
        for value in inputs.values():
            if isinstance(value, str):
                if not value.strip():
                    return True
                continue
            if isinstance(value, list):
                if not value or any(
                    not isinstance(item, str) or not item.strip() for item in value
                ):
                    return True
                continue
            return True
        return False

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[RagasErrorType, bool]:
        """将外部异常映射为稳定分类，并决定是否允许一次重试。"""
        if isinstance(exc, (TimeoutError, openai.APITimeoutError, httpx.TimeoutException)):
            return RagasErrorType.TIMEOUT, True
        if isinstance(exc, openai.RateLimitError):
            return RagasErrorType.RATE_LIMIT, True
        if isinstance(exc, (openai.APIConnectionError, httpx.NetworkError)):
            return RagasErrorType.TEMPORARY_PROVIDER, True
        if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
            return RagasErrorType.TEMPORARY_PROVIDER, True
        if isinstance(exc, InstructorRetryException):
            return RagasErrorType.PARSE_ERROR, False
        if isinstance(exc, (ValueError, TypeError, AttributeError)):
            return RagasErrorType.PARSE_ERROR, False
        return RagasErrorType.PROVIDER_ERROR, False

    @staticmethod
    def _error_outcome(
        metric_name: RagasMetricName,
        error_type: RagasErrorType,
    ) -> _MetricOutcome:
        """构建不携带异常正文的单项失败结果。"""
        return _MetricOutcome(
            score=None,
            error=RagasMetricError(metric=metric_name, error_type=error_type),
        )
