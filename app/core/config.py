from functools import lru_cache
from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    app_name: str = "rag-kb"
    app_env: str = "local"
    debug: bool = Field(default=False, validation_alias="APP_DEBUG")
    api_v1_prefix: str = "/api/v1"

    secret_key: str
    access_token_expire_minutes: int = 1440

    database_url: str
    sync_database_url: str
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    redis_url: str = "redis://localhost:6379/0"
    embedding_cache_ttl_seconds: int = 604800
    query_cache_ttl_seconds: int = 600
    query_cache_timeout_seconds: float = Field(default=1.0, gt=0)
    query_cache_max_retries: int = Field(default=1, ge=0)
    token_stats_timeout_seconds: float = Field(default=1.0, gt=0)
    token_stats_max_retries: int = Field(default=1, ge=0)
    token_budget_daily_cny: Decimal = Field(default=Decimal("1.00"), gt=0)
    token_budget_timezone: str = "Asia/Shanghai"
    token_request_alert_cost_cny: Decimal = Field(default=Decimal("0.01"), gt=0)
    prometheus_base_url: str = "http://prometheus:9090"
    prometheus_query_timeout_seconds: float = Field(default=5.0, gt=0)

    embedding_input_cost_cny_per_1k_tokens: Decimal = Field(default=Decimal("0"), ge=0)
    chat_input_cost_cny_per_1k_tokens: Decimal = Field(default=Decimal("0"), ge=0)
    chat_output_cost_cny_per_1k_tokens: Decimal = Field(default=Decimal("0"), ge=0)
    reranker_cost_cny_per_1k_tokens: Decimal = Field(default=Decimal("0"), ge=0)

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "rag-documents"
    minio_secure: bool = False

    dashscope_api_key: str = Field(default="", repr=False)
    openai_api_key: str = Field(default="", repr=False)
    openai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_model: str = "qwen-plus"
    embedding_api_key: str = Field(default="", repr=False)
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v3"
    embedding_dimension: int = 1024
    embedding_batch_size: int = 10
    embedding_cache_version: str = "v1"
    embedding_timeout_seconds: int = 30
    embedding_max_retries: int = 3
    chat_temperature: float = 0.1
    chat_max_tokens: int = 2048
    ragas_max_tokens: int = Field(default=4096, gt=0)
    ragas_timeout_seconds: float = Field(default=60, gt=0)
    chat_stream_timeout_seconds: float = Field(default=60, gt=0)

    reranker_endpoint: str
    reranker_model: str = "qwen3-rerank"
    reranker_timeout_ms: int = 800
    reranker_max_retries: int = Field(default=1, ge=0, le=2)
    reranker_top_n: int = 5

    rag_chunk_size: int = 512
    rag_chunk_overlap: int = 64
    rag_vector_top_k: int = 20
    rag_fulltext_top_k: int = 20
    rag_return_top_n: int = 5
    rag_min_score: float = 0.5
    rag_rrf_k: int = 60
    rag_query_pipeline: str = "v4"
    rag_context_max_tokens: int = 3000
    rag_faithfulness_sample_rate: float = Field(default=0.2, ge=0, le=1)
    rag_faithfulness_timeout_seconds: float = Field(default=5.0, gt=0)
    evaluation_rag_concurrency: int = Field(default=4, ge=1, le=16)

    max_upload_file_size_mb: int = 50
    max_upload_request_size_mb: int = 100

    mineru_enabled: bool = True
    mineru_mode: str = "sdk"
    mineru_api_type: str = "precision"
    mineru_api_url: str = ""
    mineru_api_key: str = Field(default="", repr=False)
    mineru_timeout_seconds: int = 300

    log_level: str = "INFO"
    enable_metrics: bool = False

    # 生产环境通过环境变量覆盖监听地址和资源上限；本地默认只监听回环地址。
    server_host: str = "127.0.0.1"
    server_port: int = Field(default=8000, ge=1, le=65535)
    server_limit_concurrency: int = Field(default=100, gt=0)
    server_timeout_keep_alive_seconds: int = Field(default=10, gt=0)
    server_timeout_graceful_shutdown_seconds: int = Field(default=30, gt=0)

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
