from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Protocol

from app.core.clients import close_clients, get_redis, init_clients
from app.core.config import get_settings

LEGACY_HYDE_PREFIX = "rag:hyde:"
HYDE_PREFIX = "rag:query:user-question-hyde:"


class RedisKeyMigrationClient(Protocol):
    """Redis 缓存 key 迁移所需的最小客户端边界。"""

    def scan_iter(self, *, match: str, count: int) -> AsyncIterator[bytes | str]: ...

    def renamenx(self, src: str, dst: str) -> Awaitable[bool]: ...


@dataclass(frozen=True)
class CacheKeyMigrationSummary:
    """Redis 缓存 key 迁移统计。"""

    planned: int
    migrated: int
    skipped: int


def build_target_key(source_key: str) -> str | None:
    """返回旧缓存 key 对应的新 key；不属于迁移范围时返回空。"""
    if source_key.startswith(LEGACY_HYDE_PREFIX):
        digest = source_key.removeprefix(LEGACY_HYDE_PREFIX)
        if len(digest) == 32 and all(char in "0123456789abcdef" for char in digest):
            return f"{HYDE_PREFIX}{digest}"

    return None


async def migrate_cache_keys(
    redis_client: RedisKeyMigrationClient,
    *,
    apply: bool,
) -> CacheKeyMigrationSummary:
    """扫描旧缓存 key，并用原子重命名迁移且保留 TTL。"""
    planned = 0
    migrated = 0
    skipped = 0
    async for raw_key in _scan_keys(redis_client, f"{LEGACY_HYDE_PREFIX}*"):
        source_key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
        target_key = build_target_key(source_key)
        if target_key is None:
            continue
        planned += 1
        if not apply:
            continue
        if await _rename_key(redis_client, source_key, target_key):
            migrated += 1
        else:
            skipped += 1
    return CacheKeyMigrationSummary(planned=planned, migrated=migrated, skipped=skipped)


async def _scan_keys(
    redis_client: RedisKeyMigrationClient,
    pattern: str,
) -> AsyncIterator[bytes | str]:
    """逐批以超时保护读取 Redis SCAN 结果。"""
    iterator = redis_client.scan_iter(match=pattern, count=100).__aiter__()
    while True:
        try:
            async with asyncio.timeout(1.0):
                yield await iterator.__anext__()
        except StopAsyncIteration:
            return


async def _rename_key(
    redis_client: RedisKeyMigrationClient,
    source: str,
    destination: str,
) -> bool:
    """以超时和一次重试原子迁移 Redis key，避免脚本无限等待。"""
    for attempt in range(2):
        try:
            async with asyncio.timeout(1.0):
                return await redis_client.renamenx(source, destination)
        except Exception:
            if attempt == 1:
                raise
    return False


async def migrate(*, apply: bool) -> None:
    """迁移 Redis 查询缓存 key；默认只预览。"""
    await init_clients(get_settings())
    try:
        summary = await migrate_cache_keys(get_redis(), apply=apply)
    finally:
        await close_clients()

    if apply:
        print(f"发现 {summary.planned} 个旧缓存 key，已迁移 {summary.migrated} 个，跳过 {summary.skipped} 个。")
    else:
        print(f"发现 {summary.planned} 个旧缓存 key；当前为预览模式，未修改 Redis。执行时请添加 --apply。")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="迁移 Redis 查询与 HyDE 缓存 key")
    parser.add_argument("--apply", action="store_true", help="实际迁移旧缓存 key；默认只预览")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(migrate(apply=parse_args().apply))
