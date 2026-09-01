from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from scripts.migrate_redis_cache_keys import build_target_key, migrate_cache_keys


class FakeRedis:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.renamenx_calls: list[tuple[str, str]] = []

    async def scan_iter(self, *, match: str, count: int) -> AsyncIterator[str]:
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key

    async def renamenx(self, source: str, destination: str) -> bool:
        self.renamenx_calls.append((source, destination))
        if source not in self.values or destination in self.values:
            return False
        self.values[destination] = self.values.pop(source)
        return True


def test_build_target_key_only_accepts_legacy_hyde_keys() -> None:
    hyde_digest = "a" * 32
    query_digest = "b" * 64

    assert build_target_key(f"rag:hyde:{hyde_digest}") == f"rag:query:user-question-hyde:{hyde_digest}"
    assert build_target_key(f"rag:query:{query_digest}") is None
    assert build_target_key("rag:query:user-question:already-migrated") is None
    assert build_target_key("rag:query:not-a-sha256") is None


@pytest.mark.asyncio
async def test_migrate_cache_keys_renames_matching_keys_without_overwriting_new_values() -> None:
    hyde_digest = "a" * 32
    query_digest = "b" * 64
    existing_digest = "c" * 64
    redis = FakeRedis(
        {
            f"rag:hyde:{hyde_digest}": "hyde value",
            f"rag:query:{query_digest}": "query value",
            f"rag:query:{existing_digest}": "old query value",
            f"rag:query:user-question:{existing_digest}": "new query value",
        }
    )

    summary = await migrate_cache_keys(redis, apply=True)

    assert summary.planned == 1
    assert summary.migrated == 1
    assert summary.skipped == 0
    assert redis.values[f"rag:query:user-question-hyde:{hyde_digest}"] == "hyde value"
    assert redis.values[f"rag:query:{query_digest}"] == "query value"
    assert redis.values[f"rag:query:{existing_digest}"] == "old query value"
