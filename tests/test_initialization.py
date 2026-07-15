import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError


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
    assert settings.rag_query_pipeline == "v4"
    assert settings.rag_faithfulness_sample_rate == 0.2
    assert settings.rag_faithfulness_timeout_seconds == 5
    assert settings.max_upload_file_size_mb == 50
    assert settings.max_upload_request_size_mb == 100


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rag_faithfulness_sample_rate", -0.1),
        ("rag_faithfulness_sample_rate", 1.1),
        ("rag_faithfulness_timeout_seconds", 0),
    ],
)
def test_settings_rejects_invalid_faithfulness_evaluation_config(field: str, value: float) -> None:
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            secret_key="test-secret",
            database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
            sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
            reranker_endpoint="https://example.test/rerank",
            **{field: value},
        )


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

    class StubAsyncOpenAI:
        def __init__(self, **kwargs):
            embedding_kwargs.update(kwargs)
            self.embeddings = object()

        async def close(self):
            return None

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
    monkeypatch.setattr(clients, "AsyncOpenAI", StubAsyncOpenAI)

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
    assert embedding_kwargs["max_retries"] == 0
    assert clients.get_embeddings().model == settings.embedding_model

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
    from uuid import UUID

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
    assert UUID(response.headers["X-Trace-Id"]).version == 4


def test_debug_reload_setting_does_not_expose_unhandled_error_details(monkeypatch):
    from uuid import UUID

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
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("不得出现在响应中的内部错误")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/crash")

    assert response.status_code == 500
    assert UUID(response.headers["X-Trace-Id"]).version == 4
    assert response.json() == {"code": 500, "message": "服务器内部错误", "data": None}
    assert "不得出现在响应中的内部错误" not in response.text


@pytest.mark.asyncio
async def test_lifespan_initializes_and_shuts_down_token_metrics(monkeypatch):
    from fastapi import FastAPI

    from app import main

    calls: list[str] = []
    fake_provider = object()
    fake_meter = object()
    fake_token_metrics = object()
    fake_faithfulness_metrics = object()

    class Provider:
        def get_meter(self, name: str):
            assert name == "rag-kb.token-metrics"
            return fake_meter

    provider = Provider()

    async def fake_init_clients(settings):
        calls.append("init_clients")

    async def fake_close_clients():
        calls.append("close_clients")

    def fake_init_metrics(settings):
        calls.append("init_metrics")
        return provider

    def fake_shutdown_metrics(value):
        assert value is provider
        calls.append("shutdown_metrics")

    def fake_token_metrics_factory(*, redis_client, meter):
        assert redis_client is fake_provider
        assert meter is fake_meter
        return fake_token_metrics

    def fake_faithfulness_metrics_factory(*, meter):
        assert meter is fake_meter
        return fake_faithfulness_metrics

    monkeypatch.setattr(main, "configure_logging", lambda settings: None)
    monkeypatch.setattr(main, "init_clients", fake_init_clients)
    monkeypatch.setattr(main, "close_clients", fake_close_clients)
    monkeypatch.setattr(main, "init_metrics", fake_init_metrics)
    monkeypatch.setattr(main, "shutdown_metrics", fake_shutdown_metrics)
    monkeypatch.setattr(main, "get_redis", lambda: fake_provider)
    monkeypatch.setattr(main, "TokenMetrics", fake_token_metrics_factory)
    monkeypatch.setattr(main, "FaithfulnessMetrics", fake_faithfulness_metrics_factory)

    app = FastAPI()
    async with main.lifespan(app):
        assert app.state.token_metrics is fake_token_metrics
        assert app.state.faithfulness_metrics is fake_faithfulness_metrics
        assert app.state.meter_provider is provider

    assert calls == ["init_clients", "init_metrics", "shutdown_metrics", "close_clients"]


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
