from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Awaitable, Protocol

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Meter

from app.core.context import current_user_var

logger = logging.getLogger(__name__)

REDIS_KEY_PREFIX = "rag:token-stats:"


class RedisTokenStore(Protocol):
    def hincrby(self, name: str, key: str, amount: int = 1) -> Awaitable[int]: ...


class GenerationTokenRecorder(Protocol):
    async def record_generation_tokens(
        self,
        *,
        tokens: int,
        source: str = "provider",
    ) -> None: ...


def extract_generation_tokens(response: Any) -> int | None:
    """从 LangChain AIMessage 兼容响应中提取 provider 返回的生成 token。"""
    usage_metadata = getattr(response, "usage_metadata", None)
    if isinstance(usage_metadata, Mapping):
        tokens = _as_non_negative_int(usage_metadata.get("output_tokens"))
        if tokens is not None:
            return tokens

    response_metadata = getattr(response, "response_metadata", None)
    if not isinstance(response_metadata, Mapping):
        return None
    token_usage = response_metadata.get("token_usage")
    if not isinstance(token_usage, Mapping):
        return None
    return _as_non_negative_int(token_usage.get("completion_tokens"))


async def record_generation_usage(
    *,
    recorder: GenerationTokenRecorder,
    response: Any,
    pipeline: str,
) -> None:
    """记录模型生成 token；provider 未返回 usage 时只写可观测日志。"""
    tokens = extract_generation_tokens(response)
    if tokens is None:
        logger.info("generation_token_usage_unavailable=true pipeline=%s", pipeline)
        return
    await recorder.record_generation_tokens(tokens=tokens)


class TokenMetrics:
    """记录 RAG 三类 token 消耗，并按用户维度累加到 Redis。"""

    def __init__(
        self,
        *,
        redis_client: RedisTokenStore,
        meter: Meter | None = None,
    ) -> None:
        self.redis = redis_client
        effective_meter = meter or metrics.get_meter("rag-kb.token-metrics")
        self._embedding_tokens = effective_meter.create_counter(
            "rag.tokens.embedding",
            description="Embedding 消耗的 Token 总数",
        )
        self._context_tokens = effective_meter.create_counter(
            "rag.tokens.context",
            description="裁剪后 RAG chunk 正文的 Context Token 总数",
        )
        self._generation_tokens = effective_meter.create_counter(
            "rag.tokens.generation",
            description="模型生成消耗的 Token 总数",
        )

    async def record_embedding_tokens(
        self,
        *,
        tokens: int,
        source: str = "provider",
    ) -> None:
        """记录 Embedding token，失败不影响调用方主流程。"""
        await self._record(
            name="embedding_tokens",
            tokens=tokens,
            counter=self._embedding_tokens,
            attributes={"source": source},
            redis_field="embeddingTokens",
        )

    async def record_context_tokens(
        self,
        *,
        tokens: int,
        pipeline: str = "v4",
    ) -> None:
        """记录裁剪后 RAG chunk 正文 token，失败不影响查询主流程。"""
        await self._record(
            name="context_tokens",
            tokens=tokens,
            counter=self._context_tokens,
            attributes={"pipeline": pipeline, "source": "local_tiktoken"},
            redis_field="contextTokens",
        )

    async def record_generation_tokens(
        self,
        *,
        tokens: int,
        source: str = "provider",
    ) -> None:
        """记录模型生成 token，失败不影响查询主流程。"""
        await self._record(
            name="generation_tokens",
            tokens=tokens,
            counter=self._generation_tokens,
            attributes={"source": source},
            redis_field="generationTokens",
        )

    async def _record(
        self,
        *,
        name: str,
        tokens: int,
        counter: Counter,
        attributes: dict[str, str],
        redis_field: str,
    ) -> None:
        """按“即时日志 -> Counter -> Redis”顺序记录单类 token。"""
        if tokens < 0:
            logger.warning("Token metric ignored: name=%s tokens=%s", name, tokens)
            return

        logger.info("[TokenMetrics] record_%s=%s", name, tokens)
        if tokens == 0:
            return

        try:
            counter.add(tokens, attributes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenTelemetry token metric write failed: name=%s error=%s", name, exc)

        user = current_user_var.get()
        if user is None:
            return

        try:
            await self.redis.hincrby(
                f"{REDIS_KEY_PREFIX}{user.user_id}",
                redis_field,
                tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis token metric write failed: user_id=%s field=%s error=%s",
                user.user_id,
                redis_field,
                exc,
            )


def _as_non_negative_int(value: object) -> int | None:
    """把 provider usage 标准化为非负整数，非法值视为不可用。"""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None
