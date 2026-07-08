from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.schemas.rag import RagQueryResponse
from app.services.rag_query import RagQueryService
from app.services.rag_query_v2 import RagQueryServiceV2
from app.services.rag_query_v3 import RagQueryServiceV3


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
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def query(self, *, question: str, kb_ids: list[int], user: CurrentUser) -> RagQueryResponse:
        self.calls.append({"question": question, "kb_ids": kb_ids, "user_id": user.user_id})
        return RagQueryResponse(answer="需要通过本地测试。[参考1]", sources=[], hit_count=1, latency_ms=12)


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


def _client(
    permission_service: FakePermissionService,
    rag_service: FakeRagQueryService,
) -> TestClient:
    from app.api.routes import rag

    app = FastAPI()
    app.include_router(rag.router, prefix="/api/v1/rag")
    app.dependency_overrides[rag.get_permission_service] = lambda: permission_service
    app.dependency_overrides[rag.get_rag_query_service] = lambda: rag_service
    app.dependency_overrides[rag.get_current_user] = lambda: _user()
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
    assert permission_service.read_checks == [2, 3]
    assert rag_service.calls == [{"question": "代码提交规范？", "kb_ids": [2, 3], "user_id": 1}]


def test_query_endpoint_stops_before_service_when_permission_denied() -> None:
    permission_service = FakePermissionService(forbidden_kb_id=3)
    rag_service = FakeRagQueryService()

    with _client(permission_service, rag_service) as client:
        response = client.post(
            "/api/v1/rag/query",
            json={"question": "代码提交规范？", "kb_ids": [2, 3]},
        )

    assert response.status_code == 403
    assert permission_service.read_checks == [2, 3]
    assert rag_service.calls == []


def test_dependency_builder_can_select_basic_query_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import rag

    monkeypatch.setattr(rag, "get_embeddings", lambda: object())
    monkeypatch.setattr(rag, "get_redis", lambda: object())
    monkeypatch.setattr(rag, "get_chat_model", lambda: object())

    service = rag.get_rag_query_service(session=object(), settings=FakeSettings(rag_query_pipeline="v1"))

    assert isinstance(service, RagQueryService)


def test_dependency_builder_can_select_hybrid_query_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import rag

    monkeypatch.setattr(rag, "get_embeddings", lambda: object())
    monkeypatch.setattr(rag, "get_redis", lambda: object())
    monkeypatch.setattr(rag, "get_chat_model", lambda: object())

    service = rag.get_rag_query_service(session=object(), settings=FakeSettings(rag_query_pipeline="v2"))

    assert isinstance(service, RagQueryServiceV2)


def test_dependency_builder_can_select_hyde_query_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import rag

    monkeypatch.setattr(rag, "get_embeddings", lambda: object())
    monkeypatch.setattr(rag, "get_redis", lambda: object())
    monkeypatch.setattr(rag, "get_chat_model", lambda: object())

    service = rag.get_rag_query_service(session=object(), settings=FakeSettings(rag_query_pipeline="v3"))

    assert isinstance(service, RagQueryServiceV3)
