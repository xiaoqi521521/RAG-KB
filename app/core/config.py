from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    reranker_endpoint: str
    reranker_model: str = "gte-rerank-v2"
    reranker_timeout_ms: int = 800
    reranker_top_n: int = 5

    rag_chunk_size: int = 512
    rag_chunk_overlap: int = 64
    rag_vector_top_k: int = 20
    rag_fulltext_top_k: int = 20
    rag_return_top_n: int = 5
    rag_min_score: float = 0.5
    rag_context_max_tokens: int = 3000

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
