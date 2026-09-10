from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
import openai
from instructor.core.exceptions import IncompleteOutputException, InstructorRetryException
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

logger = logging.getLogger(__name__)


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
    OUTPUT_TRUNCATED = "output_truncated"
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


@dataclass(frozen=True)
class RagasUsage:
    """一次评估运行中 RAGAS 判定的 provider 用量。"""

    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    embedding_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.llm_prompt_tokens + self.llm_completion_tokens + self.embedding_tokens


@dataclass
class _RagasUsageAccumulator:
    """跨单题累计一次评估运行的 RAGAS 判定 provider usage。"""

    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    embedding_tokens: int = 0

    def add_llm(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        self.llm_prompt_tokens += prompt_tokens
        self.llm_completion_tokens += completion_tokens

    def add_embedding(self, *, total_tokens: int) -> None:
        self.embedding_tokens += total_tokens

    def snapshot(self) -> RagasUsage:
        return RagasUsage(
            llm_prompt_tokens=self.llm_prompt_tokens,
            llm_completion_tokens=self.llm_completion_tokens,
            embedding_tokens=self.embedding_tokens,
        )


class _OpenAICompatibleRagasEmbeddings(BaseRagasEmbedding):
    """将项目现有异步 Embedding 客户端接入 RAGAS 现代接口。"""

    def __init__(
        self,
        embeddings: OpenAICompatibleEmbeddings,
        usage: _RagasUsageAccumulator,
    ) -> None:
        super().__init__()
        self.embeddings = embeddings
        self.usage = usage

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
        vectors, total_tokens = await self.embeddings.aembed_documents_with_usage(texts)
        if total_tokens is not None:
            self.usage.add_embedding(total_tokens=total_tokens)
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


class RagasEvaluationMetrics:
    """记录不含业务正文和标识的 RAGAS 结构化日志。"""

    def record_retry(
        self,
        *,
        metric: RagasMetricName,
        error_type: RagasErrorType,
    ) -> None:
        """记录一次低基数重试原因。"""
        logger.info(
            "ragas_metric_retry=true metric=%s error_type=%s",
            metric.value,
            error_type.value,
        )

    def record_result(
        self,
        *,
        metric: RagasMetricName,
        error_type: RagasErrorType | None,
        elapsed_ms: int,
    ) -> None:
        """记录一次单项最终结果和总耗时。"""
        result = "success" if error_type is None else "error"
        error = error_type.value if error_type is not None else "none"
        logger.info(
            "ragas_metric_completed=true metric=%s result=%s error_type=%s elapsed_ms=%s",
            metric.value,
            result,
            error,
            elapsed_ms,
        )


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
        max_retries: int = 1,
        observability: RagasEvaluationMetrics | None = None,
        usage: _RagasUsageAccumulator | None = None,
    ) -> None:
        """初始化四项指标依赖。"""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(max_retries, bool) or max_retries not in {0, 1}:
            raise ValueError("max_retries must be 0 or 1")
        self.metrics = metrics
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.observability = observability or RagasEvaluationMetrics()
        self._usage = usage or _RagasUsageAccumulator()

    @property
    def usage(self) -> RagasUsage:
        """返回本次评估运行累计的 RAGAS 判定 provider 用量。"""
        return self._usage.snapshot()

    @classmethod
    def from_clients(
        cls,
        *,
        chat_model: Any,
        embeddings: OpenAICompatibleEmbeddings,
        max_tokens: int,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        observability: RagasEvaluationMetrics | None = None,
    ) -> RagasEvaluator:
        """复用现有回答模型和 Embedding 客户端构建正式评估适配器。

        Args:
            max_tokens: RAGAS 结构化输出的独立 token 上限。
        """
        model_name = getattr(chat_model, "model_name", None)
        root_async_client = getattr(chat_model, "root_async_client", None)
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("chat_model must expose a nonblank model_name")
        if root_async_client is None:
            raise ValueError("chat_model must expose root_async_client")

        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")

        # 禁用 SDK 内部重试，确保重试次数只由本适配器控制。
        evaluation_client = root_async_client.with_options(max_retries=0)
        model_kwargs: dict[str, Any] = {"max_retries": 0}
        # 正式评估使用独立预算，避免 Faithfulness 的详细 NLI 输出被问答预算截断。
        model_kwargs["max_tokens"] = max_tokens
        usage = _RagasUsageAccumulator()
        completions = evaluation_client.chat.completions
        original_create = completions.create

        async def tracking_create(*args: Any, **kwargs: Any) -> Any:
            response = await original_create(*args, **kwargs)
            response_usage = getattr(response, "usage", None)
            usage.add_llm(
                # 缺失或 None 的用量按 0 处理，禁止伪造 provider 未报告的数字。
                prompt_tokens=getattr(response_usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(response_usage, "completion_tokens", 0) or 0,
            )
            return response

        # 先包装底层补全调用再交给 RAGAS 工厂二次包装，判定消耗才能全部经过累计。
        completions.create = tracking_create
        ragas_embeddings = _OpenAICompatibleRagasEmbeddings(embeddings, usage=usage)
        llm = llm_factory(model_name, client=evaluation_client, **model_kwargs)
        return cls(
            metrics=build_ragas_metrics(
                llm=llm,
                embeddings=ragas_embeddings,
            ),
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            observability=observability,
            usage=usage,
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
        started_at = time.perf_counter()

        def finish(outcome: _MetricOutcome) -> _MetricOutcome:
            self.observability.record_result(
                metric=metric_name,
                error_type=outcome.error.error_type if outcome.error is not None else None,
                elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            )
            return outcome

        if self._has_invalid_input(inputs):
            return finish(self._error_outcome(metric_name, RagasErrorType.INVALID_INPUT))

        for attempt in range(self.max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    metric.ascore(**inputs),
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                error_type, retryable = self._classify_error(exc)
                if attempt < self.max_retries and retryable:
                    self.observability.record_retry(
                        metric=metric_name,
                        error_type=error_type,
                    )
                    continue
                return finish(self._error_outcome(metric_name, error_type))
            else:
                try:
                    score = float(getattr(result, "value"))
                except (AttributeError, TypeError, ValueError):
                    return finish(
                        self._error_outcome(metric_name, RagasErrorType.PARSE_ERROR)
                    )
                if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                    return finish(
                        self._error_outcome(metric_name, RagasErrorType.INVALID_SCORE)
                    )
                return finish(_MetricOutcome(score=score, error=None))

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
        if isinstance(exc, IncompleteOutputException):
            return RagasErrorType.OUTPUT_TRUNCATED, False
        if isinstance(exc, InstructorRetryException):
            if isinstance(exc.__cause__, Exception):
                return RagasEvaluator._classify_error(exc.__cause__)
            if exc.failed_attempts:
                return RagasEvaluator._classify_error(exc.failed_attempts[-1].exception)
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
