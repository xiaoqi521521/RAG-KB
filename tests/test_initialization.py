import pytest
from fastapi.testclient import TestClient


def test_settings_env_file_uses_project_root_path():
    from pathlib import Path

    from app.core.config import Settings

    env_file = Path(Settings.model_config["env_file"])

    assert env_file.is_absolute()
    assert env_file == Path(__file__).resolve().parents[1] / ".env"


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
    assert settings.embedding_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.embedding_model == "text-embedding-v3"
    assert settings.rag_vector_top_k == 20
    assert settings.reranker_model == "qwen3-rerank"
    assert settings.reranker_timeout_ms == 800
    assert settings.max_upload_file_size_mb == 50
    assert settings.max_upload_request_size_mb == 100


def test_configure_logging_suppresses_verbose_http_client_logs():
    import logging

    from app.core.config import Settings
    from app.core.logging import configure_logging

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
    )

    configure_logging(settings)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


async def test_init_clients_uses_separate_chat_and_embedding_openai_configs(monkeypatch):
    from app.core import clients
    from app.core.config import Settings

    chat_kwargs = {}
    embedding_kwargs = {}

    class StubRedis:
        async def aclose(self):
            return None

    class StubChatOpenAI:
        def __init__(self, **kwargs):
            chat_kwargs.update(kwargs)

    class StubOpenAIEmbeddings:
        def __init__(self, **kwargs):
            embedding_kwargs.update(kwargs)

    class StubMinio:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class StubRedisModule:
        @staticmethod
        def from_url(*args, **kwargs):
            return StubRedis()

    monkeypatch.setattr(clients, "redis", StubRedisModule)
    monkeypatch.setattr(clients, "Minio", StubMinio)
    monkeypatch.setattr(clients, "ChatOpenAI", StubChatOpenAI)
    monkeypatch.setattr(clients, "OpenAIEmbeddings", StubOpenAIEmbeddings)

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
        openai_api_key="chat-key",
        openai_base_url="https://llm.example.test/v1",
        embedding_api_key="embedding-key",
        embedding_base_url="https://embedding.example.test/v1",
        minio_endpoint="http://localhost:9000",
    )

    await clients.init_clients(settings)

    assert clients._clients["minio"].kwargs["endpoint"] == "localhost:9000"
    assert chat_kwargs["api_key"] == "chat-key"
    assert chat_kwargs["base_url"] == "https://llm.example.test/v1"
    assert embedding_kwargs["api_key"] == "embedding-key"
    assert embedding_kwargs["base_url"] == "https://embedding.example.test/v1"
    assert embedding_kwargs["check_embedding_ctx_length"] is False

    await clients.close_clients()


def test_current_user_contextvar_is_set_and_reset():
    from app.core.context import CurrentUser, current_user_var, get_current_user_from_context

    user = CurrentUser(
        user_id=1,
        department_id="engineering",
        role="ADMIN",
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


def test_start_runs_uvicorn_with_application_entrypoint(monkeypatch):
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
    monkeypatch.setenv("APP_DEBUG", "true")

    from app.core.config import get_settings
    from app import main

    run_kwargs = {}

    def fake_run(app_path, **kwargs):
        run_kwargs["app_path"] = app_path
        run_kwargs.update(kwargs)

    get_settings.cache_clear()
    monkeypatch.setattr(main.uvicorn, "run", fake_run)

    main.start()

    assert run_kwargs == {
        "app_path": "app.main:app",
        "host": "127.0.0.1",
        "port": 8000,
        "reload": True,
        "log_level": "info",
    }
