import pytest
from fastapi.testclient import TestClient


def test_settings_loads_required_rag_defaults():
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
    )

    assert settings.app_name == "rag-kb"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.chat_model == "qwen-plus"
    assert settings.embedding_model == "text-embedding-v3"
    assert settings.rag_vector_top_k == 20
    assert settings.reranker_timeout_ms == 800
    assert settings.max_upload_file_size_mb == 50
    assert settings.max_upload_request_size_mb == 100


def test_current_user_contextvar_is_set_and_reset():
    from app.core.context import CurrentUser, current_user_var, get_current_user_from_context

    user = CurrentUser(
        user_id=1,
        username="alice",
        tenant_id="tenant-a",
        department_id="engineering",
        role="ADMIN",
        allowed_kb_ids=(1, 2),
    )

    with pytest.raises(RuntimeError, match="CurrentUser is not initialized"):
        get_current_user_from_context()

    token = current_user_var.set(user)
    try:
        assert get_current_user_from_context() == user
        assert get_current_user_from_context().is_admin is True
    finally:
        current_user_var.reset(token)

    with pytest.raises(RuntimeError, match="CurrentUser is not initialized"):
        get_current_user_from_context()


def test_fastapi_health_endpoint(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
    )
    monkeypatch.setenv(
        "SYNC_DATABASE_URL",
        "postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
    )
    monkeypatch.setenv("RERANKER_ENDPOINT", "https://example.test/rerank")

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "UP"}
