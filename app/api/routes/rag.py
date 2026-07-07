from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.routes.knowledge_bases import get_permission_service
from app.core.clients import get_chat_model, get_embeddings, get_redis
from app.core.config import Settings, get_settings
from app.core.context import CurrentUser
from app.core.database import get_db
from app.repositories.chunks import ChunkRepository
from app.schemas.common import ApiResponse
from app.schemas.rag import RagQueryRequest, RagQueryResponse
from app.services.embedding import EmbeddingConfig, EmbeddingService
from app.services.hybrid_retriever import HybridRetriever
from app.services.permissions import PermissionService
from app.services.rag_query import RagQueryService
from app.services.rag_query_v2 import RagQueryServiceV2
from app.services.source_builder import SourceBuilder
from app.services.ts_query_builder import TsQueryBuilder

router = APIRouter()


class RagQueryPipeline(Protocol):
    """路由层只依赖查询管道协议，具体版本由依赖组装决定。"""

    async def query(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
    ) -> RagQueryResponse: ...


def get_rag_query_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RagQueryPipeline:
    """根据配置构建 RAG 查询服务及其依赖。

    Args:
        session: 当前请求注入的异步数据库会话。
        settings: 应用配置，用于检索 TopK、上下文预算和模型参数。

    Returns:
        已组装依赖的查询管道。`v1` 为基础向量 RAG，`v2` 为混合检索 RAG。
    """
    embedding_config = EmbeddingConfig(
        dimension=settings.embedding_dimension,
        batch_size=settings.embedding_batch_size,
        cache_version=settings.embedding_cache_version,
        cache_ttl_seconds=settings.embedding_cache_ttl_seconds,
        max_retries=settings.embedding_max_retries,
    )
    chunk_repository = ChunkRepository(session)
    embedding_service = EmbeddingService(get_embeddings(), get_redis(), embedding_config)
    source_builder = SourceBuilder(max_context_chars=settings.rag_context_max_tokens * 4)
    chat_model = get_chat_model()

    if settings.rag_query_pipeline == "v1":
        return RagQueryService(
            embedding_service=embedding_service,
            chunk_repository=chunk_repository,
            source_builder=source_builder,
            chat_model=chat_model,
            settings=settings,
        )

    if settings.rag_query_pipeline != "v2":
        raise ValueError("rag_query_pipeline must be 'v1' or 'v2'")

    return RagQueryServiceV2(
        retriever=HybridRetriever(
            embedding_service=embedding_service,
            chunk_repository=chunk_repository,
            ts_query_builder=TsQueryBuilder(),
            settings=settings,
        ),
        source_builder=source_builder,
        chat_model=chat_model,
        settings=settings,
    )


@router.post("/query")
async def query_rag(
    request: RagQueryRequest,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    rag_service: RagQueryPipeline = Depends(get_rag_query_service),
) -> ApiResponse[RagQueryResponse]:
    """执行基础 RAG 查询。

    Args:
        request: 用户问题、目标知识库列表和可选会话 ID。
        user: 当前认证用户上下文。
        permission_service: 知识库权限服务，用于检索前硬校验读权限。
        rag_service: 查询编排服务，用于完成向量检索和回答生成。

    Returns:
        包含回答、引用来源、命中数量和总耗时的统一响应。
    """
    # 读权限必须在向量化和检索之前完成，避免无权限知识库内容进入 Prompt。
    for kb_id in request.kb_ids:
        await permission_service.require_read(kb_id, user)

    return ApiResponse.ok(
        await rag_service.query(
            question=request.question,
            kb_ids=request.kb_ids,
            user=user,
        )
    )
