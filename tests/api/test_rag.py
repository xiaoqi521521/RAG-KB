from __future__ import annotations

from dataclasses import dataclass
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.repositories.chunks import ChunkSearchHit
from app.schemas.query_cache import QueryCacheEntry
from app.schemas.rag import RagQueryResponse, SourceCitation
from app.services.enhanced_retriever import EnhancedRetrieveResult
from app.services.faithfulness_evaluator import FaithfulnessMetrics
from app.services.rag_query import RAG_REFUSAL_ANSWER, RagQueryService
from app.services.rag_query_v2 import RagQueryServiceV2
from app.services.rag_query_v3 import RagQueryServiceV3
from app.services.rag_query_v4 import RagQueryServiceV4
from app.services.reranker import RerankResult
from app.services.source_builder import SourceBuilder


def _user() -> CurrentUser:
    return CurrentUser(user_id=1, department_id="engineering", role="ADMIN")


class FakePermissionService:
    def __init__(self, forbidden_kb_id: int | None = None) -> None:
        self.forbidden_kb_id = forbidden_kb_id
        self.read_checks: list[int] = []

    async def require_read(self, kb_id: int, user: CurrentUser) -> None:
        self.read_checks.append(kb_id)
        if kb_id == self.forbidden_kb_id:
            raise HTTPException(status_code=403, detail="无权访问该知识库")


class FakeRagQueryService:
    def __init__(self, *, return_sources: bool = True) -> None:
        self.calls: list[dict[str, object]] = []
        self.return_sources = return_sources

    async def query(
        self, *, question: str, kb_ids: list[int], user: CurrentUser
    ) -> RagQueryResponse:
        self.calls.append({"question": question, "kb_ids": kb_ids, "user_id": user.user_id})
        sources = [
                SourceCitation(
                    reference_index=1,
                    document_id=1,
                    document_name="研发规范.md",
                    kb_id=2,
                    chunk_id=10,
                    chunk_index=3,
                    page_number=None,
                    section_title="代码提交",
                    excerpt="代码提交前必须通过本地测试。",
                    score=0.91,
                )
            ] if self.return_sources else []
        return RagQueryResponse(
            answer=("需要通过本地测试。[参考1]" if self.return_sources else "在知识库中未找到相关内容。"),
            sources=sources,
            hit_count=len(sources),
            latency_ms=12,
        )


class FakeQueryCache:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, tuple[int, ...]], QueryCacheEntry] = {}
        self.get_calls: list[tuple[str, list[int]]] = []
        self.put_calls: list[tuple[str, list[int]]] = []

    async def get(self, question: str, kb_ids: list[int]) -> QueryCacheEntry | None:
        self.get_calls.append((question, kb_ids))
        return self.entries.get((question.strip(), tuple(sorted(kb_ids))))

    async def put(self, question: str, kb_ids: list[int], response: RagQueryResponse) -> None:
        self.put_calls.append((question, kb_ids))
        if response.sources:
            self.entries[(question.strip(), tuple(sorted(kb_ids)))] = QueryCacheEntry(
                version=2,
                answer=response.answer,
                sources=response.sources,
                hit_count=response.hit_count,
            )


class RecordingCacheRedis:
    def __init__(self, *, fail_get: bool = False) -> None:
        self.fail_get = fail_get
        self.values: dict[str, str] = {}
        self.setex_calls: list[tuple[str, int, str]] = []

    async def get(self, key: str) -> str | None:
        if self.fail_get:
            raise RuntimeError("redis unavailable")
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.setex_calls.append((key, ttl, value))
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class FakeTokenMetrics:
    async def record_embedding_tokens(self, *, tokens: int, source: str = "provider") -> None:
        return None

    async def record_generation_tokens(self, *, tokens: int, source: str = "provider") -> None:
        return None

    async def record_context_tokens(self, *, tokens: int, pipeline: str = "v4") -> None:
        return None


class FakeTokenBudgetGate:
    async def ensure_available(self) -> None:
        return None

    @asynccontextmanager
    async def request_scope(self):
        yield


@dataclass
class FakeSettings:
    rag_query_pipeline: str
    chat_model: str = "qwen-plus"
    query_cache_ttl_seconds: int = 600
    embedding_dimension: int = 1024
    embedding_batch_size: int = 10
    embedding_cache_version: str = "v1"
    embedding_cache_ttl_seconds: int = 604800
    embedding_max_retries: int = 3
    rag_context_max_tokens: int = 3000
    rag_vector_top_k: int = 20
    rag_fulltext_top_k: int = 20
    rag_return_top_n: int = 5
    rag_min_score: float = 0.5
    rag_rrf_k: int = 60
    reranker_timeout_ms: int = 800
    reranker_max_retries: int = 1
    reranker_top_n: int = 5
    rag_faithfulness_sample_rate: float = 0.2
    rag_faithfulness_timeout_seconds: float = 5


def _client(
    permission_service: FakePermissionService,
    rag_service: FakeRagQueryService,
    query_cache: FakeQueryCache | None = None,
) -> TestClient:
    from app.api.routes import rag

    app = FastAPI()
    app.include_router(rag.router, prefix="/api/v1/rag")
    app.dependency_overrides[rag.get_permission_service] = lambda: permission_service
    app.dependency_overrides[rag.get_rag_query_service] = lambda: rag_service
    app.dependency_overrides[rag.get_current_user] = lambda: _user()
    app.dependency_overrides[rag.get_query_cache_service] = lambda: query_cache or FakeQueryCache()
    app.dependency_overrides[rag.get_token_budget_gate] = FakeTokenBudgetGate
    return TestClient(app)


def test_query_endpoint_checks_unique_kb_read_permissions_before_service_call() -> None:
    permission_service = FakePermissionService()
    rag_service = FakeRagQueryService()

    with _client(permission_service, rag_service) as client:
        response = client.post(
            "/api/v1/rag/query",
            json={"question": " 代码提交规范？ ", "kb_ids": [2, 2, 3]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["answer"] == "需要通过本地测试。[参考1]"
    assert response.json()["data"]["sources"][0]["reference_index"] == 1
    assert response.json()["data"]["sources"][0]["excerpt"] == "代码提交前必须通过本地测试。"
    assert permission_service.read_checks == [2, 3]
    assert rag_service.calls == [{"question": "代码提交规范？", "kb_ids": [2, 3], "user_id": 1}]


def test_query_endpoint_stops_before_service_when_permission_denied() -> None:
    permission_service = FakePermissionService(forbidden_kb_id=3)
    rag_service = FakeRagQueryService()
    query_cache = FakeQueryCache()

    with _client(permission_service, rag_service, query_cache) as client:
        response = client.post(
            "/api/v1/rag/query",
            json={"question": "代码提交规范？", "kb_ids": [2, 3]},
        )

    assert response.status_code == 403
    assert permission_service.read_checks == [2, 3]
    assert rag_service.calls == []
    assert query_cache.get_calls == []


def test_query_endpoint_reuses_successful_first_turn_cache() -> None:
    permission_service = FakePermissionService()
    rag_service = FakeRagQueryService()
    query_cache = FakeQueryCache()

    with _client(permission_service, rag_service, query_cache) as client:
        first = client.post(
            "/api/v1/rag/query",
            json={"question": " 代码提交规范？ ", "kb_ids": [3, 2, 2]},
        )
        second = client.post(
            "/api/v1/rag/query",
            json={"question": "代码提交规范？", "kb_ids": [2, 3]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"]["answer"] == first.json()["data"]["answer"]
    assert len(rag_service.calls) == 1
    assert len(query_cache.get_calls) == 2
    assert query_cache.put_calls == [("代码提交规范？", [3, 2])]


def test_query_endpoint_does_not_cache_refusal() -> None:
    from app.services.query_cache import QueryCacheService

    permission_service = FakePermissionService()
    rag_service = FakeRagQueryService(return_sources=False)
    redis = RecordingCacheRedis()
    query_cache = QueryCacheService(redis, ttl_seconds=600)

    with _client(permission_service, rag_service, query_cache) as client:
        response = client.post(
            "/api/v1/rag/query",
            json={"question": "没有相关内容？", "kb_ids": [2]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["sources"] == []
    assert redis.setex_calls == []


def test_query_endpoint_degrades_when_cache_read_fails() -> None:
    from app.services.query_cache import QueryCacheService

    permission_service = FakePermissionService()
    rag_service = FakeRagQueryService()
    redis = RecordingCacheRedis(fail_get=True)
    query_cache = QueryCacheService(redis, ttl_seconds=600, max_retries=1)

    with _client(permission_service, rag_service, query_cache) as client:
        response = client.post(
            "/api/v1/rag/query",
            json={"question": "缓存读取失败也应回答", "kb_ids": [2]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["answer"] == "需要通过本地测试。[参考1]"
    assert len(rag_service.calls) == 1


def test_dependency_builder_can_select_basic_query_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import rag

    monkeypatch.setattr(rag, "get_embeddings", lambda: object())
    monkeypatch.setattr(rag, "get_redis", lambda: object())
    monkeypatch.setattr(rag, "get_chat_model", lambda: object())

    token_metrics = FakeTokenMetrics()
    service = rag.get_rag_query_service(
        session=object(),
        settings=FakeSettings(rag_query_pipeline="v1"),
        token_metrics=token_metrics,
        faithfulness_metrics=FaithfulnessMetrics(),
    )

    assert isinstance(service, RagQueryService)
    assert service.token_metrics is token_metrics
    assert service.embedding_service.token_metrics is token_metrics


def test_dependency_builder_can_select_hybrid_query_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import rag

    monkeypatch.setattr(rag, "get_embeddings", lambda: object())
    monkeypatch.setattr(rag, "get_redis", lambda: object())
    monkeypatch.setattr(rag, "get_chat_model", lambda: object())

    token_metrics = FakeTokenMetrics()
    service = rag.get_rag_query_service(
        session=object(),
        settings=FakeSettings(rag_query_pipeline="v2"),
        token_metrics=token_metrics,
        faithfulness_metrics=FaithfulnessMetrics(),
    )

    assert isinstance(service, RagQueryServiceV2)
    assert service.token_metrics is token_metrics


def test_dependency_builder_can_select_hyde_query_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import rag

    monkeypatch.setattr(rag, "get_embeddings", lambda: object())
    monkeypatch.setattr(rag, "get_redis", lambda: object())
    monkeypatch.setattr(rag, "get_chat_model", lambda: object())

    token_metrics = FakeTokenMetrics()
    service = rag.get_rag_query_service(
        session=object(),
        settings=FakeSettings(rag_query_pipeline="v3"),
        token_metrics=token_metrics,
        faithfulness_metrics=FaithfulnessMetrics(),
    )

    assert isinstance(service, RagQueryServiceV3)
    assert service.token_metrics is token_metrics


def test_dependency_builder_can_select_reranker_query_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import rag

    monkeypatch.setattr(rag, "get_embeddings", lambda: object())
    monkeypatch.setattr(rag, "get_redis", lambda: object())
    monkeypatch.setattr(rag, "get_chat_model", lambda: object())

    token_metrics = FakeTokenMetrics()
    faithfulness_metrics = FaithfulnessMetrics()
    service = rag.get_rag_query_service(
        session=object(),
        settings=FakeSettings(rag_query_pipeline="v4"),
        token_metrics=token_metrics,
        faithfulness_metrics=faithfulness_metrics,
    )

    assert isinstance(service, RagQueryServiceV4)
    assert service.token_metrics is token_metrics
    assert service.context_trimmer.token_metrics is token_metrics
    assert service.context_trimmer.max_context_tokens == 3000
    assert service.faithfulness_evaluator is not None
    assert service.faithfulness_evaluator.metrics is faithfulness_metrics


def test_dependency_builder_does_not_construct_reranker_for_hyde_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import rag

    def fail_if_constructed(*args: object, **kwargs: object) -> object:
        raise AssertionError("v3 must not construct reranker dependencies")

    monkeypatch.setattr(rag, "get_embeddings", lambda: object())
    monkeypatch.setattr(rag, "get_redis", lambda: object())
    monkeypatch.setattr(rag, "get_chat_model", lambda: object())
    monkeypatch.setattr(rag, "DashScopeRerankerClient", fail_if_constructed)

    service = rag.get_rag_query_service(
        session=object(),
        settings=FakeSettings(rag_query_pipeline="v3"),
        token_metrics=FakeTokenMetrics(),
        faithfulness_metrics=FaithfulnessMetrics(),
    )

    assert isinstance(service, RagQueryServiceV3)


def test_get_token_metrics_reads_application_state() -> None:
    from app.api.routes import rag

    token_metrics = FakeTokenMetrics()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(token_metrics=token_metrics))
    )

    assert rag.get_token_metrics(request) is token_metrics


def test_get_faithfulness_metrics_reads_application_state() -> None:
    from app.api.routes import rag

    faithfulness_metrics = FaithfulnessMetrics()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(faithfulness_metrics=faithfulness_metrics))
    )

    assert rag.get_faithfulness_metrics(request) is faithfulness_metrics


@pytest.mark.parametrize(
    ("model_answer", "expected_answer", "expected_chunk_ids"),
    [
        ("第二条内容有效（来源：[参考2]）。", "第二条内容有效（来源：[参考1]）。", [11]),
        ("两条内容都需要参考。", "两条内容都需要参考。", [10, 11]),
        ("第二条有效（来源：[参考2][参考99]）。", "第二条有效（来源：[参考1][参考99]）。", [11]),
        ("引用编号错误（来源：[参考99]）。", "引用编号错误（来源：[参考99]）。", []),
        ("在知识库中未找到相关内容。", RAG_REFUSAL_ANSWER, []),
    ],
)
def test_query_endpoint_returns_v4_citation_and_refusal_outcomes(
    model_answer: str,
    expected_answer: str,
    expected_chunk_ids: list[int],
) -> None:
    hits = [
        ChunkSearchHit(
            chunk_id=10,
            doc_id=1,
            document_name="研发规范.md",
            kb_id=2,
            chunk_index=1,
            content="第一条内容。",
            page_num=None,
            section_title="代码提交",
            score=0.91,
        ),
        ChunkSearchHit(
            chunk_id=11,
            doc_id=1,
            document_name="研发规范.md",
            kb_id=2,
            chunk_index=2,
            content="第二条内容。",
            page_num=None,
            section_title="代码提交",
            score=0.86,
        ),
    ]

    class Retriever:
        async def retrieve(self, *, question: str, kb_ids: list[int]) -> EnhancedRetrieveResult:
            return EnhancedRetrieveResult(
                hits=hits,
                original_count=2,
                hyde_count=0,
                merged_count=2,
                degraded_reasons=(),
            )

    class Reranker:
        async def rerank(
            self,
            *,
            question: str,
            candidates: list[ChunkSearchHit],
        ) -> RerankResult:
            return RerankResult(
                hits=candidates,
                degraded=False,
                degraded_reason=None,
                input_count=2,
                output_count=2,
                elapsed_ms=1,
                total_tokens=10,
            )

    class ConfidenceFilter:
        def filter(self, candidates: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
            return candidates

    class ContextTrimmer:
        async def trim(self, candidates: list[ChunkSearchHit]) -> list[ChunkSearchHit]:
            return candidates

    class ChatModel:
        async def ainvoke(self, messages: list[object]) -> SimpleNamespace:
            return SimpleNamespace(
                content=model_answer,
                usage_metadata=None,
            )

    service = RagQueryServiceV4(
        retriever=Retriever(),
        reranker=Reranker(),
        confidence_filter=ConfidenceFilter(),
        context_trimmer=ContextTrimmer(),
        source_builder=SourceBuilder(max_context_chars=1000),
        chat_model=ChatModel(),
        token_metrics=FakeTokenMetrics(),
        settings=FakeSettings(rag_query_pipeline="v4", rag_return_top_n=2),
    )

    with _client(FakePermissionService(), service) as client:
        response = client.post(
            "/api/v1/rag/query",
            json={"question": "哪条内容有效？", "kb_ids": [2]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["answer"] == expected_answer
    assert response.json()["data"]["hit_count"] == len(expected_chunk_ids)
    assert [source["chunk_id"] for source in response.json()["data"]["sources"]] == (
        expected_chunk_ids
    )
