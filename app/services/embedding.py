from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any, Protocol

import httpx
import redis.asyncio as redis
from langchain_openai import OpenAIEmbeddings
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 模块基础异常，供索引任务统一识别向量化失败。"""


class EmbeddingInputError(EmbeddingError):
    """输入文本非法时抛出，调用方应修正入参而不是重试。"""


class EmbeddingProviderError(EmbeddingError):
    """外部 Embedding 服务失败或返回结果不满足契约时抛出。"""


class EmbeddingCacheError(EmbeddingError):
    """Redis 缓存值无法解析或向量校验失败时抛出。"""


class EmbeddingClient(Protocol):
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        ...

    async def aembed_query(self, text: str) -> list[float]:
        ...


class EmbeddingConfig(BaseSettings):
    """Embedding 服务配置，直接从环境变量或 .env 文件读取并由 Pydantic 校验。

    Args:
        dimension: 向量维度，必须与建库和查询使用的模型维度一致。
        batch_size: 单次调用 Embedding API 的文本数量。
        cache_version: 缓存版本号，用于模型或切分策略变更后的缓存隔离。
        cache_ttl_seconds: Redis 缓存过期时间。
        max_retries: provider 临时失败时允许的最大尝试次数。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="EMBEDDING_",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    dimension: int = Field(default=1024, gt=0, validation_alias="EMBEDDING_DIMENSION")
    batch_size: int = Field(default=10, gt=0, validation_alias="EMBEDDING_BATCH_SIZE")
    cache_version: str = Field(
        default="v1",
        min_length=1,
        validation_alias="EMBEDDING_CACHE_VERSION",
    )
    cache_ttl_seconds: int = Field(
        default=604800,
        gt=0,
        validation_alias="EMBEDDING_CACHE_TTL_SECONDS",
    )
    max_retries: int = Field(default=3, gt=0, validation_alias="EMBEDDING_MAX_RETRIES")

    @model_validator(mode="after")
    def validate_bounds(self) -> "EmbeddingConfig":
        """校验跨字段或字符串清洗类约束，返回合法配置对象。"""
        if not self.cache_version.strip():
            # 空白版本会让不同配置共享同一批 key，必须作为参数错误提前拦截。
            raise ValueError("embedding cache_version must not be blank")
        return self


class EmbeddingVectorCodec:
    """负责 Redis 向量缓存的序列化、反序列化和维度校验。"""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def dumps(self, vector: list[float]) -> str:
        """将向量序列化为 JSON 字符串，返回可写入 Redis 的 value。"""
        normalized = self._validate_vector(vector)
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

    def loads(self, value: str | bytes) -> list[float]:
        """解析 Redis value 并返回合法向量，脏缓存会转换为 EmbeddingCacheError。"""
        try:
            raw_value = value.decode("utf-8") if isinstance(value, bytes) else value
            parsed = json.loads(raw_value)
            if not isinstance(parsed, list):
                raise EmbeddingCacheError("cached embedding value must be a JSON list")
            return self._validate_vector(parsed)
        except EmbeddingCacheError:
            raise
        except EmbeddingProviderError as exc:
            # 缓存里的维度或数值异常属于脏缓存，不应按 provider 失败中断整批索引。
            raise EmbeddingCacheError("cached embedding vector is invalid") from exc
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            # 历史版本或被截断的缓存值无法信任，统一走 API 重算路径。
            raise EmbeddingCacheError("cached embedding value is invalid JSON") from exc

    def _validate_vector(self, vector: list[Any]) -> list[float]:
        """校验向量维度和数值类型，返回标准化后的 float 列表。"""
        if len(vector) != self.dimension:
            raise EmbeddingProviderError(
                f"embedding dimension mismatch: expected {self.dimension}, got {len(vector)}"
            )

        normalized: list[float] = []
        for item in vector:
            try:
                value = float(item)
            except (TypeError, ValueError) as exc:
                raise EmbeddingProviderError("embedding vector contains non-numeric value") from exc
            if not math.isfinite(value):
                # NaN/Infinity 无法可靠写入 PGVector，也会污染相似度计算。
                raise EmbeddingProviderError("embedding vector contains non-finite value")
            normalized.append(value)
        return normalized


class EmbeddingService:
    """批量向量化服务，封装 Redis 缓存、批处理、重试和结果顺序恢复。"""

    def __init__(
        self,
        embeddings: EmbeddingClient | OpenAIEmbeddings,
        redis_client: redis.Redis,
        config: EmbeddingConfig | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.redis = redis_client
        self.config = config or EmbeddingConfig()
        self.codec = EmbeddingVectorCodec(self.config.dimension)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文本列表，返回与输入顺序完全一致的向量列表。"""
        if not texts:
            return []

        # 第一步：标准化文本并构造缓存 key，后续所有命中和 miss 都按 key 聚合。
        started_at = time.perf_counter()
        normalized_texts = [_normalize_text(text) for text in texts]
        cache_keys = [self.build_cache_key(text) for text in normalized_texts]

        vectors_by_index: dict[int, list[float]] = {}
        miss_by_key: dict[str, str] = {}
        key_indices: dict[str, list[int]] = {}
        dirty_cache_count = 0
        cache_hit_count = 0
        cache_miss_count = 0

        # 第二步：读取 Redis 缓存，命中结果先按原始下标暂存。
        cached_values = await self._read_cache(cache_keys)
        for index, (text, cache_key, cached_value) in enumerate(
            zip(normalized_texts, cache_keys, cached_values, strict=True)
        ):
            key_indices.setdefault(cache_key, []).append(index)
            if cached_value is None:
                cache_miss_count += 1
                miss_by_key.setdefault(cache_key, text)
                continue

            try:
                vectors_by_index[index] = self.codec.loads(cached_value)
                cache_hit_count += 1
            except EmbeddingCacheError as exc:
                # 单个 key 损坏不应拖垮整批任务，删除脏 key 后按 miss 重新生成。
                dirty_cache_count += 1
                logger.warning("Embedding cache is dirty, fallback to API: key=%s error=%s", cache_key, exc)
                await self._delete_dirty_key(cache_key)
                cache_miss_count += 1
                miss_by_key.setdefault(cache_key, text)

        # 第三步：只对未命中的唯一文本调用 provider，再把结果复制回所有原始下标。
        api_batch_count = 0
        api_elapsed_ms = 0.0
        if miss_by_key:
            miss_items = list(miss_by_key.items())
            for batch in _batched(miss_items, self.config.batch_size):
                api_batch_count += 1
                batch_keys = [item[0] for item in batch]
                batch_texts = [item[1] for item in batch]

                batch_started_at = time.perf_counter()
                batch_vectors = await self._embed_batch_with_retry(batch_texts)
                api_elapsed_ms += (time.perf_counter() - batch_started_at) * 1000

                for cache_key, vector in zip(batch_keys, batch_vectors, strict=True):
                    for original_index in key_indices[cache_key]:
                        vectors_by_index[original_index] = vector
                    await self._write_cache(cache_key, vector)

        # 第四步：按输入顺序组装最终结果，防止缓存命中和 API miss 打乱 chunk 对应关系。
        try:
            vectors = [vectors_by_index[index] for index in range(len(normalized_texts))]
        except KeyError as exc:
            raise EmbeddingProviderError("embedding result is incomplete") from exc

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        cache_hit_rate = cache_hit_count / len(texts) * 100
        logger.info(
            "Embedding completed: texts=%s cache_hits=%s cache_misses=%s cache_hit_rate=%.2f%% dirty_cache=%s api_batches=%s elapsed_ms=%.2f api_elapsed_ms=%.2f token_usage_unavailable=%s",
            len(texts),
            cache_hit_count,
            cache_miss_count,
            cache_hit_rate,
            dirty_cache_count,
            api_batch_count,
            elapsed_ms,
            api_elapsed_ms,
            True,
        )
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """向量化单条查询文本，返回可用于 PGVector 检索的向量。"""
        vectors = await self.embed_documents([text])
        return vectors[0]

    def build_cache_key(self, normalized_text: str) -> str:
        """根据缓存版本和文本内容构造短且稳定的 Redis key。"""
        digest = hashlib.md5(normalized_text.encode("utf-8")).hexdigest()
        return f"emb:{self.config.cache_version}:{digest}"

    async def _read_cache(self, keys: list[str]) -> list[str | bytes | None]:
        """批量读取 Redis 缓存，失败时整体降级为全部 miss。"""
        try:
            pipe = self.redis.pipeline()
            for key in keys:
                pipe.get(key)
            result = await pipe.execute()
            return list(result)
        except Exception as exc:  # noqa: BLE001
            # 缓存是加速层，读取异常不能影响本次向量化的主流程。
            logger.warning("Embedding cache read failed, downgrade to full miss: error=%s", exc)
            return [None] * len(keys)

    async def _write_cache(self, key: str, vector: list[float]) -> None:
        """写入 Redis 缓存并设置 TTL，写失败只记录日志。"""
        try:
            await self.redis.setex(key, self.config.cache_ttl_seconds, self.codec.dumps(vector))
        except Exception as exc:  # noqa: BLE001
            # 写缓存失败不影响已拿到的 provider 结果，避免让加速层变成可靠性瓶颈。
            logger.warning("Embedding cache write failed: key=%s error=%s", key, exc)

    async def _delete_dirty_key(self, key: str) -> None:
        """删除反序列化失败的脏缓存，删除失败只记录日志。"""
        try:
            await self.redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding dirty cache delete failed: key=%s error=%s", key, exc)

    async def _embed_batch_with_retry(self, texts: list[str]) -> list[list[float]]:
        """带重试调用 provider，并校验返回数量和向量维度。"""
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.config.max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception(_is_retryable_provider_error),
                reraise=True,
            ):
                with attempt:
                    vectors = await self.embeddings.aembed_documents(texts)
                    self._validate_batch_result(texts, vectors)
                    return vectors
        except EmbeddingProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            # provider SDK、网络层或重试耗尽的异常统一包成业务可识别的 provider 错误。
            raise EmbeddingProviderError(f"Embedding API call failed: {exc}") from exc

        raise EmbeddingProviderError("Embedding API did not return a result")

    def _validate_batch_result(self, texts: list[str], vectors: list[list[float]]) -> None:
        """校验 provider 返回数量和每个向量的维度，防止错位结果入库。"""
        if len(vectors) != len(texts):
            raise EmbeddingProviderError(
                f"embedding result count mismatch: expected {len(texts)}, got {len(vectors)}"
            )
        for vector in vectors:
            self.codec._validate_vector(vector)


def _normalize_text(text: str) -> str:
    """规范化单条输入文本，返回用于缓存和 API 请求的文本。"""
    if not isinstance(text, str):
        raise EmbeddingInputError("embedding text must be a string")
    normalized = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        raise EmbeddingInputError("embedding text must not be blank")
    return normalized


def _batched(items: list[tuple[str, str]], batch_size: int) -> list[list[tuple[str, str]]]:
    """按固定大小切分列表，返回 provider 批量调用所需的批次。"""
    return [items[start : start + batch_size] for start in range(0, len(items), batch_size)]


def _is_retryable_provider_error(exc: BaseException) -> bool:
    """判断 provider 异常是否适合重试，避免无意义重复调用 4xx 配置错误。"""
    if isinstance(exc, EmbeddingProviderError):
        return False
    if isinstance(
        exc,
        (APIConnectionError, APITimeoutError, RateLimitError, httpx.TimeoutException, TimeoutError),
    ):
        return True
    if isinstance(exc, APIStatusError):
        # 429 和 5xx 通常是限流或服务端临时问题，适合有限重试。
        return exc.status_code == 429 or exc.status_code >= 500
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code == 429 or status_code >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    return False
