from typing import Any

import redis.asyncio as redis
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from minio import Minio

from app.core.config import Settings

_clients: dict[str, Any] = {}


async def init_clients(settings: Settings) -> None:
    _clients["redis"] = redis.from_url(settings.redis_url, decode_responses=True)
    _clients["minio"] = Minio(
        endpoint=settings.minio_endpoint,
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
        )

    embedding_api_key = settings.embedding_api_key or settings.dashscope_api_key
    if embedding_api_key:
        _clients["embeddings"] = OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=embedding_api_key,
            base_url=settings.embedding_base_url,
            timeout=settings.embedding_timeout_seconds,
        )


async def close_clients() -> None:
    redis_client = _clients.get("redis")
    if redis_client is not None:
        await redis_client.aclose()
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


def get_embeddings() -> OpenAIEmbeddings:
    try:
        return _clients["embeddings"]
    except KeyError as exc:
        raise RuntimeError("Embedding client is not configured") from exc
