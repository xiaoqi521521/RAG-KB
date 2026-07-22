from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import Counter, Histogram, Meter

from app.services.token_metrics import GenerationTokenRecorder, record_generation_usage

logger = logging.getLogger(__name__)

_RESULT_PATTERN = re.compile(
    r"^\s*忠实性分数\s*[:：]\s*(10|[0-9])\s*\n"
    r"\s*是否忠实\s*[:：]\s*(是|否)\s*"
    r"(?:\n\s*理由\s*[:：]\s*(\S(?:.*\S)?)\s*)?$"
)


class FaithfulnessStatus(str, Enum):
    """忠实性评估的内部状态。"""

    FAITHFUL = "faithful"
    UNFAITHFUL = "unfaithful"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class FaithfulnessResult:
    """一次忠实性抽样评估的结构化结果。"""

    status: FaithfulnessStatus
    score: float | None
    reason: str | None
    elapsed_ms: int
    sampled: bool


class FaithfulnessMetrics:
    """记录忠实性评估的状态、分数和耗时指标。"""

    def __init__(self, *, meter: Meter | None = None) -> None:
        """初始化指标 Instrument；未启用监控时使用 OpenTelemetry 的空实现。"""
        effective_meter = meter or otel_metrics.get_meter("rag-kb.faithfulness")
        self._evaluations: Counter = effective_meter.create_counter(
            "rag.faithfulness.evaluations",
            description="忠实性评估次数",
        )
        self._scores: Histogram = effective_meter.create_histogram(
            "rag.faithfulness.score",
            description="忠实性评估分数",
        )
        self._durations: Histogram = effective_meter.create_histogram(
            "rag.faithfulness.duration",
            unit="ms",
            description="忠实性评估耗时",
        )

    def record(self, result: FaithfulnessResult) -> None:
        """写入一次评估观测；指标故障不应影响回答链路。"""
        attributes = {
            "status": result.status.value,
            "sampled": str(result.sampled).lower(),
        }
        try:
            self._evaluations.add(1, attributes)
            self._durations.record(result.elapsed_ms, attributes)
            if result.score is not None:
                self._scores.record(result.score, attributes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("faithfulness_metric_write_failed=true error_type=%s", type(exc).__name__)


class FaithfulnessEvaluator:
    """对回答执行非阻断的抽样忠实性评估。"""

    def __init__(
        self,
        *,
        chat_model: Any,
        token_metrics: GenerationTokenRecorder,
        sampling_rate: float,
        timeout_seconds: float,
        metrics: FaithfulnessMetrics | None = None,
        model_name: str = "unknown",
    ) -> None:
        """初始化复用回答模型的评估器。"""
        self.chat_model = chat_model
        self.token_metrics = token_metrics
        self.sampling_rate = sampling_rate
        self.timeout_seconds = timeout_seconds
        self.metrics = metrics
        self.model_name = model_name

    async def evaluate(
        self,
        *,
        question: str,
        answer: str,
        context: str,
        kb_id: str | int = "unknown",
    ) -> FaithfulnessResult:
        """评估回答是否有参考内容依据，失败时返回 error 而非抛出异常。"""
        started_at = time.perf_counter()
        if not self._should_sample():
            result = FaithfulnessResult(
                status=FaithfulnessStatus.SKIPPED,
                score=None,
                reason=None,
                elapsed_ms=self._elapsed_ms(started_at),
                sampled=False,
            )
            self._log_result(result)
            self._record_metrics(result)
            return result

        try:
            response = await asyncio.wait_for(
                self.chat_model.ainvoke(self._build_messages(question, answer, context)),
                timeout=self.timeout_seconds,
            )
            # 评估消费与正常回答分开计量，便于观测质量治理的额外成本。
            await self._record_token_usage(response, kb_id=kb_id)
            result = self._parse_result(response, started_at)
        except asyncio.TimeoutError:
            result = self._error_result(started_at, "timeout")
        except Exception as exc:  # noqa: BLE001
            logger.warning("faithfulness_evaluation_failed=true error_type=%s", type(exc).__name__)
            result = self._error_result(started_at, type(exc).__name__)

        self._log_result(result)
        self._record_metrics(result)
        return result

    def _should_sample(self) -> bool:
        """根据抽样率决定是否调用评估模型。"""
        if self.sampling_rate <= 0:
            return False
        if self.sampling_rate >= 1:
            return True
        return random.random() < self.sampling_rate

    def _build_messages(self, question: str, answer: str, context: str) -> list[object]:
        """构建严格且只基于参考内容的评估提示。"""
        return [
            SystemMessage(
                content=(
                    "你是企业知识库回答忠实性评估器。只能依据给出的参考内容，"
                    "判断回答中的事实是否都有依据，不得使用外部知识。"
                    "只按以下格式输出：\n"
                    "忠实性分数：0-10 的整数\n"
                    "是否忠实：是或否\n"
                    "理由：一句话说明（可省略）"
                )
            ),
            HumanMessage(
                content=f"问题：\n{question}\n\n回答：\n{answer}\n\n参考内容：\n{context}"
            ),
        ]

    def _parse_result(self, response: object, started_at: float) -> FaithfulnessResult:
        """严格解析评估模型输出，避免把自然语言猜测为有效结果。"""
        content = getattr(response, "content", None)
        if not isinstance(content, str):
            return self._error_result(started_at, "invalid_response_content")

        matched = _RESULT_PATTERN.fullmatch(content)
        if matched is None:
            return self._error_result(started_at, "invalid_response_format")

        score = int(matched.group(1)) / 10
        status = (
            FaithfulnessStatus.FAITHFUL
            if matched.group(2) == "是"
            else FaithfulnessStatus.UNFAITHFUL
        )
        return FaithfulnessResult(
            status=status,
            score=score,
            reason=matched.group(3),
            elapsed_ms=self._elapsed_ms(started_at),
            sampled=True,
        )

    async def _record_token_usage(self, response: object, *, kb_id: str | int) -> None:
        """记录评估额外生成 Token，记录故障不影响评估结果。"""
        try:
            await record_generation_usage(
                recorder=self.token_metrics,
                response=response,
                pipeline="faithfulness_evaluation",
                source="faithfulness_evaluation",
                model=self.model_name,
                kb_id=kb_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("faithfulness_token_metric_failed=true error_type=%s", type(exc).__name__)

    def _error_result(self, started_at: float, reason: str) -> FaithfulnessResult:
        """统一构建不带分数的评估失败结果。"""
        return FaithfulnessResult(
            status=FaithfulnessStatus.ERROR,
            score=None,
            reason=reason,
            elapsed_ms=self._elapsed_ms(started_at),
            sampled=True,
        )

    def _log_result(self, result: FaithfulnessResult) -> None:
        """记录不含问题、回答和参考内容的观测日志。"""
        logger.info(
            "faithfulness_evaluation_completed=true status=%s score=%s elapsed_ms=%s sampled=%s",
            result.status.value,
            result.score,
            result.elapsed_ms,
            result.sampled,
        )

    def _record_metrics(self, result: FaithfulnessResult) -> None:
        """将评估结果交给应用级指标记录器。"""
        if self.metrics is not None:
            self.metrics.record(result)

    def _elapsed_ms(self, started_at: float) -> int:
        """返回非负的评估耗时毫秒数。"""
        return max(0, int((time.perf_counter() - started_at) * 1000))
