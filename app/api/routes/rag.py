from __future__ import annotations

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
from app.services.permissions import PermissionService
from app.services.rag_query import RagQueryService
from app.services.source_builder import SourceBuilder

router = APIRouter()


def get_rag_query_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RagQueryService:
    """构建基础 RAG 查询服务及其依赖。

    Args:
        session: 当前请求注入的异步数据库会话。
        settings: 应用配置，用于检索 TopK、上下文预算和模型参数。

    Returns:
        已组装 Embedding、chunk 检索、来源构建和聊天模型的 RagQueryService。
    """
    embedding_config = EmbeddingConfig(
        dimension=settings.embedding_dimension,
        batch_size=settings.embedding_batch_size,
        cache_version=settings.embedding_cache_version,
        cache_ttl_seconds=settings.embedding_cache_ttl_seconds,
        max_retries=settings.embedding_max_retries,
    )
    return RagQueryService(
        embedding_service=EmbeddingService(get_embeddings(), get_redis(), embedding_config),
        chunk_repository=ChunkRepository(session),
        source_builder=SourceBuilder(max_context_chars=settings.rag_context_max_tokens * 4),
        chat_model=get_chat_model(),
        settings=settings,
    )


@router.post("/query")
async def query_rag(
    request: RagQueryRequest,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    rag_service: RagQueryService = Depends(get_rag_query_service),
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
