from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

class FakePipeline:
    def __init__(self, redis_client: "FakeRedis") -> None:
        self.redis_client = redis_client
        self.ops: list[tuple[str, str]] = []

    def get(self, key: str) -> "FakePipeline":
        self.ops.append(("get", key))
        return self

    async def execute(self) -> list[str | None]:
        return [self.redis_client.store.get(key) for op, key in self.ops]


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.pipeline_calls = 0
        self.set_calls: list[tuple[str, int, str]] = []
        self.deleted_keys: list[str] = []
        self.fail_setex = False
        self.fail_delete = False

    def pipeline(self) -> FakePipeline:
        self.pipeline_calls += 1
        return FakePipeline(self)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.set_calls.append((key, ttl, value))
        if self.fail_setex:
            raise RuntimeError("setex failed")
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.deleted_keys.append(key)
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.store.pop(key, None)


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.result_map: dict[tuple[str, ...], list[list[float]]] = {}
        self.fail_sequence: list[Exception] = []

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail_sequence:
            exc = self.fail_sequence.pop(0)
            raise exc
        key = tuple(texts)
        try:
            return self.result_map[key]
        except KeyError as exc:
            raise AssertionError(f"missing fake embedding result for {key}") from exc

    async def aembed_query(self, text: str) -> list[float]:
        result = await self.aembed_documents([text])
        return result[0]


def _build_service(
    *,
    embedding_map: dict[tuple[str, ...], list[list[float]]] | None = None,
    config: object | None = None,
) -> tuple[object, FakeEmbeddings, FakeRedis]:
    from app.services.embedding import EmbeddingConfig, EmbeddingService

    fake_embeddings = FakeEmbeddings()
    fake_embeddings.result_map = embedding_map or {}
    fake_redis = FakeRedis()
    service = EmbeddingService(fake_embeddings, fake_redis, config or EmbeddingConfig())
    return service, fake_embeddings, fake_redis


def _vector(value: float) -> list[float]:
    return [value] * 1024


def test_embed_documents_returns_empty_list_for_empty_input():
    service, embeddings, redis_client = _build_service()

    assert asyncio_run(service.embed_documents([])) == []


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_embed_documents_rejects_blank_input():
    from app.services.embedding import EmbeddingInputError

    service, _, _ = _build_service()

    with pytest.raises(EmbeddingInputError, match="must not be blank"):
        asyncio_run(service.embed_documents(["   "]))


def test_embed_documents_uses_cache_and_preserves_order():
    service, embeddings, redis_client = _build_service(
        embedding_map={
            ("报销单据需在费用发生后 30 天内提交。",): [_vector(0.2)],
        }
    )

    cached_key = service.build_cache_key("员工每年享有 5 天带薪年假。")
    assert cached_key.startswith("rag:emb:doc:v1:")
    redis_client.store[cached_key] = service.codec.dumps(_vector(0.1))

    result = asyncio_run(
        service.embed_documents(
            [
                "员工每年享有 5 天带薪年假。",
                "报销单据需在费用发生后 30 天内提交。",
                "员工每年享有 5 天带薪年假。",
            ]
        )
    )

    assert result == [_vector(0.1), _vector(0.2), _vector(0.1)]
    assert embeddings.calls == [["报销单据需在费用发生后 30 天内提交。"]]
    assert redis_client.set_calls == [
        (service.build_cache_key("报销单据需在费用发生后 30 天内提交。"), 604800, service.codec.dumps(_vector(0.2)))
    ]


def test_embed_documents_logs_cache_hit_rate(caplog):
    service, _, redis_client = _build_service(
        embedding_map={
            ("报销单据需在费用发生后 30 天内提交。",): [_vector(0.2)],
        }
    )
    cached_key = service.build_cache_key("员工每年享有 5 天带薪年假。")
    redis_client.store[cached_key] = service.codec.dumps(_vector(0.1))

    with caplog.at_level(logging.INFO, logger="app.services.embedding"):
        asyncio_run(
            service.embed_documents(
                [
                    "员工每年享有 5 天带薪年假。",
                    "报销单据需在费用发生后 30 天内提交。",
                    "员工每年享有 5 天带薪年假。",
                ]
            )
        )

    assert "namespace=doc" in caplog.text
    assert "cache_hit_rate=66.67%" in caplog.text


def test_embed_documents_removes_dirty_cache_and_rebuilds():
    service, embeddings, redis_client = _build_service(
        embedding_map={("坏缓存文本",): [_vector(0.3)]}
    )

    key = service.build_cache_key("坏缓存文本")
    redis_client.store[key] = "not-json"

    result = asyncio_run(service.embed_documents(["坏缓存文本"]))

    assert result == [_vector(0.3)]
    assert redis_client.deleted_keys == [key]
    assert embeddings.calls == [["坏缓存文本"]]


def test_embed_documents_batches_miss_texts_by_configured_batch_size():
    from app.services.embedding import EmbeddingConfig

    service, embeddings, _ = _build_service(
        embedding_map={
            ("t1", "t2"): [_vector(0.1), _vector(0.2)],
            ("t3", "t4"): [_vector(0.3), _vector(0.4)],
            ("t5",): [_vector(0.5)],
        },
        config=EmbeddingConfig(batch_size=2),
    )

    result = asyncio_run(service.embed_documents(["t1", "t2", "t3", "t4", "t5"]))

    assert result == [_vector(0.1), _vector(0.2), _vector(0.3), _vector(0.4), _vector(0.5)]
    assert embeddings.calls == [["t1", "t2"], ["t3", "t4"], ["t5"]]


def test_embed_query_does_not_use_cache_or_write_completion_log(caplog):
    service, embeddings, redis_client = _build_service(
        embedding_map={("query text",): [_vector(0.9)]}
    )

    with caplog.at_level(logging.INFO, logger="app.services.embedding"):
        result = asyncio_run(service.embed_query("query text"))

    assert result == _vector(0.9)
    assert embeddings.calls == [["query text"]]
    assert redis_client.pipeline_calls == 0
    assert redis_client.store == {}
    assert redis_client.set_calls == []
    assert "Embedding completed" not in caplog.text


def test_embed_query_can_disable_cache_for_hyde_namespace(caplog):
    service, embeddings, redis_client = _build_service(
        embedding_map={("hyde text",): [_vector(0.6)]}
    )

    with caplog.at_level(logging.INFO, logger="app.services.embedding"):
        result = asyncio_run(service.embed_query("hyde text", namespace="hyde", cache_enabled=False))

    assert result == _vector(0.6)
    assert embeddings.calls == [["hyde text"]]
    assert redis_client.store == {}
    assert redis_client.set_calls == []
    assert "Embedding completed" not in caplog.text


def test_embed_documents_raises_on_incomplete_provider_result():
    from app.services.embedding import EmbeddingProviderError

    service, embeddings, _ = _build_service(
        embedding_map={("a", "b"): [_vector(0.1)]}
    )

    with pytest.raises(EmbeddingProviderError, match="count mismatch"):
        asyncio_run(service.embed_documents(["a", "b"]))


def test_embed_documents_retries_transient_errors():
    from app.services.embedding import EmbeddingConfig

    service, embeddings, _ = _build_service(
        embedding_map={("retry me",): [_vector(0.7)]},
        config=EmbeddingConfig(max_retries=3),
    )
    embeddings.fail_sequence = [TimeoutError("timeout")]

    result = asyncio_run(service.embed_documents(["retry me"]))

    assert result == [_vector(0.7)]
    assert embeddings.calls == [["retry me"], ["retry me"]]


def test_embed_batch_uses_fixed_exponential_wait_strategy(monkeypatch):
    from tenacity import wait_exponential

    captured_kwargs: dict[str, object] = {}

    class FakeAttempt:
        def __enter__(self) -> None:
            return None

        def __exit__(self, exc_type, exc_value, traceback) -> bool:
            return False

    class FakeAsyncRetrying:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)
            self._yielded = False

        def __aiter__(self) -> "FakeAsyncRetrying":
            return self

        async def __anext__(self) -> FakeAttempt:
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return FakeAttempt()

    monkeypatch.setattr("app.services.embedding.AsyncRetrying", FakeAsyncRetrying)
    service, _, _ = _build_service(
        embedding_map={("retry wait",): [_vector(0.4)]}
    )

    result = asyncio_run(service._embed_batch_with_retry(["retry wait"]))

    assert result == [_vector(0.4)]
    assert isinstance(captured_kwargs["wait"], wait_exponential)


def test_embed_documents_does_not_retry_non_transient_provider_error():
    from app.services.embedding import EmbeddingConfig, EmbeddingProviderError

    service, embeddings, _ = _build_service(
        embedding_map={("bad request",): [_vector(0.1)]},
        config=EmbeddingConfig(max_retries=3),
    )
    embeddings.fail_sequence = [EmbeddingProviderError("bad request")]

    with pytest.raises(EmbeddingProviderError, match="bad request"):
        asyncio_run(service.embed_documents(["bad request"]))

    assert embeddings.calls == [["bad request"]]


def test_cache_write_failure_does_not_break_success_result():
    service, embeddings, redis_client = _build_service(
        embedding_map={("persist me",): [_vector(0.8)]}
    )
    redis_client.fail_setex = True

    result = asyncio_run(service.embed_documents(["persist me"]))

    assert result == [_vector(0.8)]
    assert embeddings.calls == [["persist me"]]


def test_embedding_config_reads_settings_from_env(monkeypatch):
    from app.services.embedding import EmbeddingConfig

    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "10")
    monkeypatch.setenv("EMBEDDING_CACHE_VERSION", "v9")
    monkeypatch.setenv("EMBEDDING_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("EMBEDDING_MAX_RETRIES", "3")
    config = EmbeddingConfig(_env_file=None)

    assert config.dimension == 1024
    assert config.batch_size == 10
    assert config.cache_version == "v9"
    assert config.max_retries == 3


def test_embedding_config_raises_validation_error_for_invalid_values():
    from app.services.embedding import EmbeddingConfig

    with pytest.raises(ValidationError, match="Input should be greater than 0"):
        EmbeddingConfig(max_retries=0)


def test_embedding_cache_error_is_raised_for_invalid_json():
    from app.services.embedding import EmbeddingCacheError

    service, _, _ = _build_service()

    with pytest.raises(EmbeddingCacheError):
        service.codec.loads("not-json")
