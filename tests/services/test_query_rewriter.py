from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.query_rewriter import QueryRewriter


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.get_calls: list[str] = []
        self.setex_calls: list[dict[str, object]] = []
        self.fail_get = False
        self.fail_setex = False

    async def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        if self.fail_get:
            raise RuntimeError("redis get failed")
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.setex_calls.append({"key": key, "ttl": ttl, "value": value})
        if self.fail_setex:
            raise RuntimeError("redis setex failed")
        self.values[key] = value


class FakeChatModel:
    def __init__(self, *contents: object) -> None:
        self.contents = list(contents)
        self.messages: list[list[object]] = []

    async def ainvoke(self, messages: list[object]) -> SimpleNamespace:
        self.messages.append(messages)
        content = self.contents.pop(0)
        if isinstance(content, BaseException):
            raise content
        return SimpleNamespace(content=content)


@pytest.mark.asyncio
async def test_generate_hyde_answer_uses_cache_without_calling_chat() -> None:
    redis = FakeRedis()
    rewriter = QueryRewriter(
        chat_model=FakeChatModel("should not be used"),
        redis_client=redis,
        chat_model_name="qwen-plus",
        cache_ttl_seconds=600,
    )
    redis.values[rewriter._cache_key("hyde", "年假怎么申请？")] = "员工需要在 OA 提交年假申请。"

    result = await rewriter.generate_hyde_answer(" 年假怎么申请？ ")

    assert result.original_question == "年假怎么申请？"
    assert result.hyde_answer == "员工需要在 OA 提交年假申请。"
    assert result.used_cache is True
    assert result.degraded_reasons == ()
    assert len(rewriter.chat_model.messages) == 0


@pytest.mark.asyncio
async def test_generate_hyde_answer_calls_chat_and_writes_cache() -> None:
    redis = FakeRedis()
    chat = FakeChatModel(" 员工申请年假需要提前在 OA 系统提交申请。 ")
    rewriter = QueryRewriter(
        chat_model=chat,
        redis_client=redis,
        chat_model_name="qwen-plus",
        cache_ttl_seconds=600,
    )

    result = await rewriter.generate_hyde_answer("年假怎么申请？")

    assert result.hyde_answer == "员工申请年假需要提前在 OA 系统提交申请。"
    assert result.used_cache is False
    assert result.degraded_reasons == ()
    assert redis.setex_calls[0]["ttl"] == 600
    assert "年假怎么申请？" in chat.messages[0][1].content


@pytest.mark.asyncio
async def test_generate_hyde_answer_degrades_when_chat_fails() -> None:
    rewriter = QueryRewriter(
        chat_model=FakeChatModel(RuntimeError("model failed")),
        redis_client=FakeRedis(),
        chat_model_name="qwen-plus",
        cache_ttl_seconds=600,
    )

    result = await rewriter.generate_hyde_answer("年假怎么申请？")

    assert result.hyde_answer is None
    assert result.used_cache is False
    assert "hyde_generation_failed" in result.degraded_reasons


@pytest.mark.asyncio
async def test_expand_queries_cleans_numbering_and_deduplicates_original_question() -> None:
    chat = FakeChatModel(
        """
        1. 年假申请流程是什么？
        2. 年假怎么申请？
        - OA 系统如何提交假期申请？
        员工请假需要哪些步骤？
        """
    )
    rewriter = QueryRewriter(
        chat_model=chat,
        redis_client=FakeRedis(),
        chat_model_name="qwen-plus",
        cache_ttl_seconds=600,
    )

    result = await rewriter.expand_queries("年假怎么申请？")

    assert result.expanded_queries == [
        "年假申请流程是什么？",
        "OA 系统如何提交假期申请？",
        "员工请假需要哪些步骤？",
    ]
    assert result.used_cache is False
    assert result.degraded_reasons == ()


@pytest.mark.asyncio
async def test_expand_queries_degrades_when_redis_read_fails_but_chat_succeeds() -> None:
    redis = FakeRedis()
    redis.fail_get = True
    rewriter = QueryRewriter(
        chat_model=FakeChatModel("年假申请流程是什么？"),
        redis_client=redis,
        chat_model_name="qwen-plus",
        cache_ttl_seconds=600,
    )

    result = await rewriter.expand_queries("年假怎么申请？")

    assert result.expanded_queries == ["年假申请流程是什么？"]
    assert "multi_cache_read_failed" in result.degraded_reasons
