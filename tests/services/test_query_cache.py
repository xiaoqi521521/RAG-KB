from __future__ import annotations

import asyncio
import json

import pytest

from app.schemas.rag import RagQueryResponse, SourceCitation
from app.services.query_cache import QueryCacheService


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.setex_calls: list[tuple[str, int, str]] = []
        self.fail_get = False
        self.fail_setex = False
        self.fail_delete = False
        self.get_calls = 0

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        if self.fail_get:
            raise RuntimeError("redis unavailable")
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        if self.fail_setex:
            raise RuntimeError("redis unavailable")
        self.setex_calls.append((key, ttl, value))
        self.values[key] = value

    async def delete(self, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("redis unavailable")
        self.values.pop(key, None)


def _response() -> RagQueryResponse:
    return RagQueryResponse(
        answer="年假需要提前申请。（来源：[参考1]）",
        sources=[
            SourceCitation(
                reference_index=1,
                document_id=1,
                document_name="员工手册.md",
                kb_id=2,
                chunk_id=10,
                chunk_index=0,
                page_number=3,
                section_title="假期",
                excerpt="年假需要提前申请。",
                score=0.9,
            )
        ],
        hit_count=1,
        latency_ms=42,
    )


@pytest.mark.asyncio
async def test_query_cache_builds_stable_key_and_round_trips_cacheable_response() -> None:
    redis = FakeRedis()
    service = QueryCacheService(redis, ttl_seconds=600)

    key = service.build_cache_key("员工手册？", [3, 2])
    assert key == "rag:query:3a6547e7469a3f9ad1abe4af53b1e826883f9615887b3aea3b3b5599c664704a"

    await service.put(" 员工手册？ ", [2, 3], _response())

    cached = await service.get("员工手册？", [3, 2])

    assert cached is not None
    assert cached.answer == "年假需要提前申请。（来源：[参考1]）"
    assert cached.sources[0].chunk_id == 10
    assert cached.hit_count == 1
    assert redis.setex_calls[0][0] == key
    assert redis.setex_calls[0][1] == 600
    assert json.loads(redis.setex_calls[0][2])["version"] == 1


@pytest.mark.asyncio
async def test_query_cache_does_not_store_refusal_without_sources() -> None:
    redis = FakeRedis()
    service = QueryCacheService(redis, ttl_seconds=600)
    refusal = RagQueryResponse(
        answer="在知识库中未找到与该问题相关的内容。",
        sources=[],
        hit_count=0,
        latency_ms=3,
    )

    await service.put("没有相关内容", [2], refusal)

    assert redis.setex_calls == []


@pytest.mark.asyncio
async def test_query_cache_treats_invalid_value_and_redis_failures_as_non_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = FakeRedis()
    service = QueryCacheService(redis, ttl_seconds=600)
    key = service.build_cache_key("坏缓存", [2])
    redis.values[key] = "not-json"

    cached = await service.get("坏缓存", [2])

    assert cached is None
    assert key not in redis.values

    redis.fail_get = True
    assert await service.get("读取失败", [2]) is None

    redis.fail_get = False
    redis.fail_setex = True
    await service.put("写入失败", [2], _response())

    assert "Query cache read failed" in caplog.text
    assert "Query cache write failed" in caplog.text


@pytest.mark.asyncio
async def test_query_cache_retries_redis_read_once_before_degrading() -> None:
    redis = FakeRedis()
    redis.fail_get = True
    service = QueryCacheService(redis, ttl_seconds=600, max_retries=1)

    assert await service.get("重试读取", [2]) is None
    assert redis.get_calls == 2


@pytest.mark.asyncio
async def test_query_cache_times_out_slow_redis_read() -> None:
    class SlowRedis(FakeRedis):
        async def get(self, key: str) -> str | None:
            await asyncio.sleep(0.05)
            return await super().get(key)

    redis = SlowRedis()
    service = QueryCacheService(redis, ttl_seconds=600, timeout_seconds=0.001)

    assert await service.get("超时读取", [2]) is None


@pytest.mark.asyncio
async def test_query_cache_rejects_missing_version_and_empty_sources() -> None:
    redis = FakeRedis()
    service = QueryCacheService(redis, ttl_seconds=600)
    key = service.build_cache_key("版本缺失", [2])
    payload = json.loads(_response().model_dump_json())
    payload["version"] = 1
    payload.pop("version")
    redis.values[key] = json.dumps(payload, ensure_ascii=False)

    assert await service.get("版本缺失", [2]) is None
    assert key not in redis.values

    empty_key = service.build_cache_key("空引用", [2])
    redis.values[empty_key] = json.dumps(
        {"version": 1, "answer": "拒答", "sources": [], "hit_count": 0},
        ensure_ascii=False,
    )

    assert await service.get("空引用", [2]) is None
    assert empty_key not in redis.values


@pytest.mark.asyncio
async def test_query_cache_rejects_inconsistent_hit_count() -> None:
    redis = FakeRedis()
    service = QueryCacheService(redis, ttl_seconds=600)
    key = service.build_cache_key("计数不一致", [2])
    payload = json.loads(_response().model_dump_json())
    payload["hit_count"] = 0
    redis.values[key] = json.dumps(payload, ensure_ascii=False)

    assert await service.get("计数不一致", [2]) is None
    assert key not in redis.values
