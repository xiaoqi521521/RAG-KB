from typing import Any
from urllib.parse import urlparse

import redis.asyncio as redis
from langchain_openai import ChatOpenAI
from minio import Minio
from openai import AsyncOpenAI

from app.core.config import Settings
from app.integrations.openai_embeddings import OpenAICompatibleEmbeddings

_clients: dict[str, Any] = {}


async def init_clients(settings: Settings) -> None:
    _clients["redis"] = redis.from_url(settings.redis_url, decode_responses=True)
    _clients["minio"] = Minio(
        endpoint=_normalize_minio_endpoint(settings.minio_endpoint),
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    chat_api_key = settings.openai_api_key or settings.dashscope_api_key
    if chat_api_key:
        _clients["chat_model"] = ChatOpenAI(
            model=settings.chat_model,
            api_key=chat_api_key,
            base_url=settings.openai_base_url,
            temperature=settings.chat_temperature,
            max_tokens=settings.chat_max_tokens,
            timeout=settings.chat_stream_timeout_seconds,
            # 重试由各业务服务统一控制，避免模型 SDK 重试叠加放大请求耗时。
            max_retries=0,
        )

    embedding_api_key = settings.embedding_api_key or settings.dashscope_api_key
    if embedding_api_key:
        embedding_openai_client = AsyncOpenAI(
            api_key=embedding_api_key,
            base_url=settings.embedding_base_url,
            timeout=settings.embedding_timeout_seconds,
            # 业务服务已经统一负责重试，避免 SDK 与服务层叠加重试。
            max_retries=0,
        )
        _clients["embedding_openai_client"] = embedding_openai_client
        _clients["embeddings"] = OpenAICompatibleEmbeddings(
            embeddings_api=embedding_openai_client.embeddings,
            model=settings.embedding_model,
        )


async def close_clients() -> None:
    redis_client = _clients.get("redis")
    if redis_client is not None:
        await redis_client.aclose()
    embedding_openai_client = _clients.get("embedding_openai_client")
    if embedding_openai_client is not None:
        await embedding_openai_client.close()
    _clients.clear()


def get_redis() -> redis.Redis:
    return _clients["redis"]


def get_minio() -> Minio:
    return _clients["minio"]


def get_chat_model() -> ChatOpenAI:
    try:
        return _clients["chat_model"]
    except KeyError as exc:
        raise RuntimeError("Chat model client is not configured") from exc


def get_embeddings() -> OpenAICompatibleEmbeddings:
    try:
        return _clients["embeddings"]
    except KeyError as exc:
        raise RuntimeError("Embedding client is not configured") from exc


def _normalize_minio_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme and parsed.netloc:
        return parsed.netloc
    return endpoint
