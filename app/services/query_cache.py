from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable
from typing import Protocol

from app.schemas.query_cache import QueryCacheEntry
from app.schemas.rag import RagQueryResponse

logger = logging.getLogger(__name__)


class QueryCacheRedis(Protocol):
    def get(self, name: bytes | str | memoryview[int]) -> Awaitable[bytes | str | None]: ...

    def setex(
        self,
        name: bytes | str | memoryview[int],
        time: int,
        value: bytes | str,
    ) -> Awaitable[object]: ...

    def delete(self, *names: bytes | str | memoryview[int]) -> Awaitable[int]: ...

    def renamenx(
        self,
        src: bytes | str | memoryview[int],
        dst: bytes | str | memoryview[int],
    ) -> Awaitable[bool]: ...


class QueryCacheService:
    """管理首轮查询结果缓存，并把 Redis 故障降级为缓存不可用。"""

    KEY_PREFIX = "rag:query:user-question:"
    LEGACY_KEY_PREFIX = "rag:query:"

    def __init__(
        self,
        redis_client: QueryCacheRedis,
        *,
        ttl_seconds: int,
        timeout_seconds: float = 1.0,
        max_retries: int = 1,
    ) -> None:
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    async def get(self, question: str, kb_ids: list[int]) -> QueryCacheEntry | None:
        """读取并校验缓存值，读取或解析失败时按未命中处理。"""
        key = self.build_cache_key(question, kb_ids)
        try:
            raw_value = await self._read(key)
            if raw_value is None:
                raw_value = await self._migrate_legacy_value(question, kb_ids, key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Query cache read failed: operation=read error_type=%s",
                type(exc).__name__,
            )
            return None

        if raw_value is None:
            logger.info("Query cache result: outcome=miss")
            return None

        try:
            value = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else raw_value
            entry = QueryCacheEntry.model_validate_json(value)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Query cache value invalid: operation=read error_type=%s",
                type(exc).__name__,
            )
            await self._delete_invalid(key)
            return None

        if not entry.sources or entry.hit_count != len(entry.sources):
            logger.warning(
                "Query cache value invalid: operation=read error_type=invalid_response_shape",
            )
            await self._delete_invalid(key)
            return None

        logger.info("Query cache result: outcome=hit")
        return entry

    async def put(
        self,
        question: str,
        kb_ids: list[int],
        response: RagQueryResponse,
    ) -> None:
        """写入带引用来源的成功回答，缓存故障不影响调用方。"""
        if not response.sources or response.hit_count != len(response.sources):
            return

        entry = QueryCacheEntry(
            version=2,
            answer=response.answer,
            sources=response.sources,
            hit_count=response.hit_count,
        )
        key = self.build_cache_key(question, kb_ids)
        try:
            await self._write(key, entry.model_dump_json())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Query cache write failed: operation=write error_type=%s",
                type(exc).__name__,
            )

    def build_cache_key(self, question: str, kb_ids: list[int]) -> str:
        """按问题和知识库范围构造不暴露原文的稳定缓存键。"""
        return self._build_cache_key(self.KEY_PREFIX, question, kb_ids)

    async def _migrate_legacy_value(
        self,
        question: str,
        kb_ids: list[int],
        key: str,
    ) -> str | bytes | None:
        """将命中的旧版查询缓存原子迁移到新 key，并保留原有 TTL。"""
        legacy_key = self._build_cache_key(self.LEGACY_KEY_PREFIX, question, kb_ids)
        legacy_value = await self._read(legacy_key)
        if legacy_value is None:
            return None
        if await self._rename(legacy_key, key):
            return legacy_value
        return await self._read(key)

    def _build_cache_key(
        self,
        prefix: str,
        question: str,
        kb_ids: list[int],
        *,
        algorithm: str = "md5",
    ) -> str:
        """按指定摘要算法构造查询缓存 key。"""
        payload = json.dumps(
            [question.strip(), sorted(kb_ids)],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.md5(payload).hexdigest() if algorithm == "md5" else hashlib.sha256(payload).hexdigest()
        return f"{prefix}{digest}"

    async def _read(self, key: str) -> str | bytes | None:
        """带超时和有限重试读取 Redis，避免缓存故障拖住查询。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    return await self.redis.get(key)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self.max_retries:
                    raise
        raise RuntimeError("query cache read failed") from last_error

    async def _write(self, key: str, value: str) -> None:
        """带超时和有限重试写入 Redis，失败时保留主流程结果。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    await self.redis.setex(key, self.ttl_seconds, value)
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self.max_retries:
                    raise
        raise RuntimeError("query cache write failed") from last_error

    async def _rename(self, source: str, destination: str) -> bool:
        """带超时和有限重试迁移 Redis key，确保不重置旧缓存 TTL。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    return await self.redis.renamenx(source, destination)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self.max_retries:
                    raise
        raise RuntimeError("query cache migration failed") from last_error

    async def _delete_invalid(self, key: str) -> None:
        """尽力删除脏值，删除失败仍保持缓存降级语义。"""
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Query cache invalid-value delete failed: operation=delete error_type=%s",
                type(exc).__name__,
            )
