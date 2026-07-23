from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.registry import REGISTRY

from app.core.context import current_user_var
from app.services.token_budget import GlobalTokenBudgetGate, TOKEN_REDIS_NAMESPACE

logger = logging.getLogger(__name__)


def _metric_or_existing(factory: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """应用测试/重载重复创建时复用默认 registry 的同名 collector。"""
    try:
        return factory(name, *args, **kwargs)
    except ValueError:
        registry = kwargs.get("registry")
        if registry is not REGISTRY:
            raise
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if existing is None:
            raise
        return existing

REDIS_KEY_PREFIX = f"{TOKEN_REDIS_NAMESPACE}stats:"
_REDIS_COST_FIELD = "estimatedCostCny"
_USER_USAGE_UPDATE_SCRIPT = """
local existing_token = redis.call('HGET', KEYS[1], ARGV[1])
if existing_token and tonumber(existing_token) == nil then
  return redis.error_reply('invalid Token field')
end
local existing_cost = redis.call('HGET', KEYS[1], ARGV[3])
if existing_cost and tonumber(existing_cost) == nil then
  return redis.error_reply('invalid estimated cost field')
end
local token_total = redis.call('HINCRBY', KEYS[1], ARGV[1], ARGV[2])
local cost_total = redis.call('HINCRBYFLOAT', KEYS[1], ARGV[3], ARGV[4])
return {token_total, cost_total}
"""
TOKEN_TYPES = (
    "embedding",
    "input",
    "answer_generation",
    "hyde",
    "reranker",
    "faithfulness_check",
)
TokenType = Literal[
    "embedding",
    "input",
    "answer_generation",
    "hyde",
    "reranker",
    "faithfulness_check",
]
_OUTPUT_TOKEN_TYPES = frozenset({"answer_generation", "hyde", "faithfulness_check"})
_REDIS_FIELDS: dict[str, str] = {
    "embedding": "embeddingTokens",
    "input": "inputTokens",
    "answer_generation": "answerGenerationTokens",
    "hyde": "hydeTokens",
    "reranker": "rerankerTokens",
    "faithfulness_check": "faithfulnessTokens",
}


@dataclass(frozen=True)
class UserTokenUsage:
    """当前用户在线请求累计的六类 Token 和金额。"""

    embedding_tokens: int
    input_tokens: int
    answer_generation_tokens: int
    hyde_tokens: int
    reranker_tokens: int
    faithfulness_tokens: int
    estimated_cost_cny: Decimal = Decimal("0")


class TokenMetricsUnavailableError(RuntimeError):
    """无法可靠读取用户 Token 统计时抛出。"""


class RedisTokenStore(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    async def hincrby(self, name: Any, key: Any, amount: int = 1) -> Any: ...

    async def hgetall(self, name: Any) -> Any: ...


class GenerationTokenRecorder(Protocol):
    """旧评估器依赖的最小生成 Token 接口。"""

    async def record_generation_tokens(
        self,
        *,
        tokens: int,
        source: str = "provider",
    ) -> None: ...


def extract_input_tokens(response: Any) -> int | None:
    """从兼容 LangChain/OpenAI 的响应中提取 provider 输入 Token。"""
    return _extract_usage(response, "input")


def extract_output_tokens(response: Any) -> int | None:
    """从兼容 LangChain/OpenAI 的响应中提取 provider 输出 Token。"""
    return _extract_usage(response, "output")


def extract_generation_tokens(response: Any) -> int | None:
    """兼容旧调用方的生成 Token 提取入口。"""
    return extract_output_tokens(response)


def extract_usage(response: Any) -> tuple[int | None, int | None]:
    """返回一次聊天响应的输入、输出 provider usage。"""
    return extract_input_tokens(response), extract_output_tokens(response)


def knowledge_base_scope(kb_ids: Sequence[int]) -> str:
    """把请求知识库范围转换为不重复计量的 Prometheus 标签。"""
    if len(kb_ids) == 1:
        return str(kb_ids[0])
    return "multi"


class TokenUsageRecorder:
    """把可靠的 provider usage 同时写入 Prometheus 和用户 Redis v3。"""

    def __init__(
        self,
        *,
        redis_client: RedisTokenStore,
        registry: CollectorRegistry | None = None,
        read_timeout_seconds: float = 1.0,
        read_max_retries: int = 1,
        budget_gate: GlobalTokenBudgetGate | None = None,
        write_timeout_seconds: float = 1.0,
        embedding_price: Decimal = Decimal("0"),
        chat_input_price: Decimal = Decimal("0"),
        chat_output_price: Decimal = Decimal("0"),
        reranker_price: Decimal = Decimal("0"),
    ) -> None:
        self.redis = redis_client
        self.registry = registry or REGISTRY
        self.read_timeout_seconds = read_timeout_seconds
        self.read_max_retries = read_max_retries
        self.budget_gate = budget_gate
        if write_timeout_seconds <= 0:
            raise ValueError("write_timeout_seconds must be positive")
        self.write_timeout_seconds = write_timeout_seconds
        self._prices = {
            "embedding": embedding_price,
            "input": chat_input_price,
            "answer_generation": chat_output_price,
            "hyde": chat_output_price,
            "faithfulness_check": chat_output_price,
            "reranker": reranker_price,
        }
        self._usage = _metric_or_existing(
            Counter,
            "rag_token_usage_total",
            "Provider reported Token usage",
            ["model", "token_type", "kb_id"],
            registry=self.registry,
        )
        self._usage_unavailable = _metric_or_existing(
            Counter,
            "rag_token_usage_unavailable_total",
            "Model calls whose provider Token usage was unavailable",
            ["model", "token_type", "kb_id"],
            registry=self.registry,
        )
        self._usage_cost = _metric_or_existing(
            Counter,
            "rag_token_usage_cost_cny_total",
            "Estimated CNY cost for provider reported Token usage",
            ["model", "token_type", "kb_id"],
            registry=self.registry,
        )
        self._write_failure = _metric_or_existing(
            Counter,
            "rag_token_write_failure_total",
            "Token usage sink write failures",
            ["sink", "token_type"],
            registry=self.registry,
        )

    async def record_chat_usage(
        self,
        *,
        response: Any,
        model: str,
        output_type: TokenType,
        kb_id: str | int,
        user_scoped: bool = True,
        budget_scoped: bool = True,
    ) -> None:
        """记录一次聊天调用的输入和指定阶段输出，两个桶互不重叠。"""
        if output_type not in _OUTPUT_TOKEN_TYPES:
            raise ValueError("chat output_type must be an output Token type")

        input_tokens = extract_input_tokens(response)
        if input_tokens is None:
            self.record_usage_unavailable(
                model=model,
                token_type="input",
                kb_id=kb_id,
            )
        else:
            await self.record_usage(
                tokens=input_tokens,
                model=model,
                token_type="input",
                kb_id=kb_id,
                user_scoped=user_scoped,
                budget_scoped=budget_scoped,
            )

        output_tokens = extract_output_tokens(response)
        if output_tokens is None:
            self.record_usage_unavailable(
                model=model,
                token_type=output_type,
                kb_id=kb_id,
            )
        else:
            await self.record_usage(
                tokens=output_tokens,
                model=model,
                token_type=output_type,
                kb_id=kb_id,
                user_scoped=user_scoped,
                budget_scoped=budget_scoped,
            )

    async def record_usage(
        self,
        *,
        tokens: int,
        model: str,
        token_type: TokenType,
        kb_id: str | int,
        user_scoped: bool = True,
        budget_scoped: bool = True,
    ) -> None:
        """记录一个已标准化的 Token 增量，任何观测出口失败都不抛出。"""
        self._validate_token_type(token_type)
        if tokens < 0:
            self.record_usage_unavailable(model=model, token_type=token_type, kb_id=kb_id)
            return
        if tokens == 0:
            return

        labels = {
            "model": _label_value(model, fallback="unknown"),
            "token_type": token_type,
            "kb_id": _label_value(kb_id, fallback="unknown"),
        }
        try:
            self._usage.labels(**labels).inc(tokens)
        except Exception as exc:  # noqa: BLE001
            self._record_write_failure(sink="prometheus", token_type=token_type)
            logger.warning(
                "Token usage Prometheus write failed: token_type=%s error_type=%s",
                token_type,
                type(exc).__name__,
            )
        estimated_cost = Decimal("0")
        try:
            price = self._prices[token_type]
            estimated_cost = Decimal(tokens) / Decimal("1000") * price
            if estimated_cost > 0:
                self._usage_cost.labels(**labels).inc(float(estimated_cost))
        except Exception as exc:  # noqa: BLE001
            self._record_write_failure(sink="prometheus", token_type=token_type)
            logger.warning(
                "Token usage cost metric write failed: token_type=%s error_type=%s",
                token_type,
                type(exc).__name__,
            )

        if self.budget_gate is not None and budget_scoped:
            await self.budget_gate.record_tokens(tokens)
            self.budget_gate.add_request_tokens(tokens)

        if not user_scoped:
            return
        user = current_user_var.get()
        if user is None:
            return

        try:
            async with asyncio.timeout(self.write_timeout_seconds):
                await self.redis.eval(
                    _USER_USAGE_UPDATE_SCRIPT,
                    1,
                    f"{REDIS_KEY_PREFIX}{user.user_id}",
                    _REDIS_FIELDS[token_type],
                    tokens,
                    _REDIS_COST_FIELD,
                    _decimal_for_redis(estimated_cost),
                )
        except Exception as exc:  # noqa: BLE001
            self._record_write_failure(sink="redis", token_type=token_type)
            logger.warning(
                "Token usage Redis write failed: token_type=%s error_type=%s",
                token_type,
                type(exc).__name__,
            )

    def record_usage_unavailable(
        self,
        *,
        model: str,
        token_type: TokenType,
        kb_id: str | int,
    ) -> None:
        """记录 provider usage 不可用的观测，不用本地估算填充 Token。"""
        self._validate_token_type(token_type)
        labels = {
            "model": _label_value(model, fallback="unknown"),
            "token_type": token_type,
            "kb_id": _label_value(kb_id, fallback="unknown"),
        }
        try:
            self._usage_unavailable.labels(**labels).inc()
        except Exception as exc:  # noqa: BLE001
            self._record_write_failure(sink="prometheus", token_type=token_type)
            logger.warning(
                "Token usage unavailable metric write failed: token_type=%s error_type=%s",
                token_type,
                type(exc).__name__,
            )
        logger.info("token_usage_unavailable=true token_type=%s", token_type)

    async def read_user_tokens(self, user_id: int) -> UserTokenUsage:
        """读取当前用户 v3 累计 Token 和金额，失败时不返回伪零值。"""
        try:
            raw_values = await self._read_hash(f"{REDIS_KEY_PREFIX}{user_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis token stats read failed: error_type=%s",
                type(exc).__name__,
            )
            raise TokenMetricsUnavailableError("Token 统计暂不可用") from exc

        try:
            if not isinstance(raw_values, Mapping):
                raise TypeError("Token stats must be a mapping")
            values: dict[str, object] = {}
            for raw_key, raw_value in raw_values.items():
                key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
                if key in _REDIS_FIELDS.values() or key == _REDIS_COST_FIELD:
                    values[key] = raw_value
            return UserTokenUsage(
                embedding_tokens=_parse_stored_token(values.get("embeddingTokens", 0)),
                input_tokens=_parse_stored_token(values.get("inputTokens", 0)),
                answer_generation_tokens=_parse_stored_token(
                    values.get("answerGenerationTokens", 0)
                ),
                hyde_tokens=_parse_stored_token(values.get("hydeTokens", 0)),
                reranker_tokens=_parse_stored_token(values.get("rerankerTokens", 0)),
                faithfulness_tokens=_parse_stored_token(values.get("faithfulnessTokens", 0)),
                estimated_cost_cny=_parse_stored_cost(values.get(_REDIS_COST_FIELD, 0)),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Redis token stats invalid: error_type=%s",
                type(exc).__name__,
            )
            raise TokenMetricsUnavailableError("Token 统计数据不可用") from exc

    def render_metrics(self) -> str:
        """渲染当前 recorder 所属 registry，主要供测试和诊断使用。"""
        return generate_latest(self.registry).decode("utf-8")

    @staticmethod
    def extract_usage(response: Any) -> tuple[int | None, int | None]:
        """公开响应 usage 提取，返回输入和输出 Token。"""
        return extract_usage(response)

    async def record_embedding_tokens(
        self,
        *,
        tokens: int,
        source: str = "provider",
    ) -> None:
        """兼容旧调用接口；新代码应显式调用 record_usage。"""
        await self.record_usage(
            tokens=tokens,
            model="unknown",
            token_type="embedding",
            kb_id="unknown",
            user_scoped=source not in {"offline_indexing", "internal"},
            budget_scoped=source == "provider",
        )

    async def record_generation_tokens(
        self,
        *,
        tokens: int,
        source: str = "provider",
    ) -> None:
        """兼容旧调用接口；默认把生成归入最终回答桶。"""
        await self.record_usage(
            tokens=tokens,
            model="unknown",
            token_type=("faithfulness_check" if source == "faithfulness_evaluation" else "answer_generation"),
            kb_id="unknown",
            user_scoped=source != "faithfulness_evaluation",
            budget_scoped=True,
        )

    async def record_context_tokens(
        self,
        *,
        tokens: int,
        pipeline: str = "v4",
    ) -> None:
        """保留旧接口但不再创建 context 桶，输入只取 provider 完整 usage。"""
        if tokens > 0:
            logger.info("legacy_context_tokens_ignored=true pipeline=%s", pipeline)

    async def _read_hash(self, name: str) -> Mapping[str | bytes, str | bytes]:
        """带超时和有限重试读取用户统计 Hash。"""
        last_error: Exception | None = None
        for attempt in range(self.read_max_retries + 1):
            try:
                async with asyncio.timeout(self.read_timeout_seconds):
                    return await self.redis.hgetall(name)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self.read_max_retries:
                    raise
        raise RuntimeError("Token stats read failed") from last_error

    def _record_write_failure(self, *, sink: str, token_type: str) -> None:
        try:
            self._write_failure.labels(sink=sink, token_type=token_type).inc()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Token usage write failure metric unavailable: sink=%s token_type=%s",
                sink,
                token_type,
            )

    @staticmethod
    def _validate_token_type(token_type: str) -> None:
        if token_type not in TOKEN_TYPES:
            raise ValueError(f"unsupported token_type: {token_type}")


# 现有服务仍以 TokenMetrics 命名注入；实现已统一到新的记录契约。
TokenMetrics = TokenUsageRecorder


async def record_chat_usage(
    *,
    recorder: TokenUsageRecorder,
    response: Any,
    model: str,
    output_type: TokenType,
    kb_id: str | int,
    user_scoped: bool = True,
) -> None:
    """使用统一 recorder 记录一次聊天调用的输入和阶段输出。"""
    await recorder.record_chat_usage(
        response=response,
        model=model,
        output_type=output_type,
        kb_id=kb_id,
        user_scoped=user_scoped,
    )


async def record_generation_usage(
    *,
    recorder: Any,
    response: Any,
    pipeline: str,
    source: str = "provider",
    model: str = "unknown",
    kb_id: str | int = "unknown",
) -> None:
    """兼容旧入口并按调用来源映射到新的聊天输出桶。"""
    output_type: TokenType = (
        "faithfulness_check" if source == "faithfulness_evaluation" else "answer_generation"
    )
    if hasattr(recorder, "record_chat_usage"):
        await recorder.record_chat_usage(
            response=response,
            model=model,
            output_type=output_type,
            kb_id=kb_id,
            user_scoped=True,
        )
        return

    tokens = extract_output_tokens(response)
    if tokens is None:
        logger.info("token_usage_unavailable=true token_type=%s pipeline=%s", output_type, pipeline)
        return
    await recorder.record_generation_tokens(tokens=tokens, source=source)


def _extract_usage(response: Any, kind: Literal["input", "output"]) -> int | None:
    """按 LangChain 优先、OpenAI 兼容字段其次的顺序提取 usage。"""
    keys = (
        ("input_tokens", "prompt_tokens")
        if kind == "input"
        else ("output_tokens", "completion_tokens")
    )
    usage_metadata = _as_mapping(getattr(response, "usage_metadata", None))
    value = _first_valid(usage_metadata, keys)
    if value is not None:
        return value

    response_metadata = _as_mapping(getattr(response, "response_metadata", None))
    for candidate in (
        _as_mapping(response_metadata.get("token_usage")),
        _as_mapping(response_metadata.get("usage")),
        response_metadata,
    ):
        value = _first_valid(candidate, keys)
        if value is not None:
            return value

    return _first_valid(_as_mapping(getattr(response, "usage", None)), keys)


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _first_valid(mapping: Mapping[str, object], keys: Sequence[str]) -> int | None:
    for key in keys:
        if key in mapping:
            normalized = _as_non_negative_int(mapping[key])
            if normalized is not None:
                return normalized
    return None


def _as_non_negative_int(value: object) -> int | None:
    """把 provider usage 标准化为非负整数，非法值视为不可用。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or not normalized.isdecimal():
            return None
        return int(normalized)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _label_value(value: object, *, fallback: str) -> str:
    normalized = str(value).strip()
    return normalized or fallback


def _parse_stored_token(value: object) -> int:
    """把 Redis Hash 中的单个 Token 字段解析为非负整数。"""
    normalized = _as_non_negative_int(value)
    if normalized is None:
        raise ValueError("Token value must be a non-negative integer")
    return normalized


def _decimal_for_redis(value: Decimal) -> str:
    """把金额转为 Redis HINCRBYFLOAT 可接受的普通小数字符串。"""
    return format(value, "f")


def _parse_stored_cost(value: object) -> Decimal:
    """把 Redis Hash 中的累计金额解析为非负 Decimal。"""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Cost value must be a Decimal") from None
    if not normalized.is_finite() or normalized < 0:
        raise ValueError("Cost value must be a finite non-negative Decimal")
    return normalized
