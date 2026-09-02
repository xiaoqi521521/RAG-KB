from __future__ import annotations

import time
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.routes.knowledge_bases import get_permission_service
from app.core.clients import get_chat_model, get_embeddings, get_redis
from app.core.config import Settings, get_settings
from app.core.context import CurrentUser
from app.core.database import get_db
from app.integrations.dashscope import DashScopeRerankerClient
from app.repositories.chunks import ChunkRepository
from app.schemas.common import ApiResponse
from app.schemas.rag import RagQueryRequest, RagQueryResponse
from app.services.confidence_filter import ConfidenceFilter
from app.services.context_trimmer import ContextTrimmer
from app.services.embedding import EmbeddingConfig, EmbeddingService
from app.services.enhanced_retriever import EnhancedRetriever
from app.services.faithfulness_evaluator import FaithfulnessEvaluator
from app.services.faithfulness_evaluator import FaithfulnessMetrics
from app.services.hybrid_retriever import HybridRetriever
from app.services.permissions import PermissionService
from app.services.query_rewriter import QueryRewriter
from app.services.rag_query import RagQueryService
from app.services.rag_query_v2 import RagQueryServiceV2
from app.services.rag_query_v3 import RagQueryServiceV3
from app.services.rag_query_v4 import RagQueryServiceV4
from app.services.reranker import RerankerService
from app.services.source_builder import SourceBuilder
from app.services.ts_query_builder import TsQueryBuilder
from app.services.token_metrics import TokenMetrics
from app.services.token_budget import (
    GlobalTokenBudgetGate,
    TokenBudgetExhaustedError,
    TokenBudgetUnavailableError,
)
from app.services.query_cache import QueryCacheService

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


def get_token_metrics(request: Request) -> TokenMetrics:
    """从应用状态获取单例 TokenMetrics。"""
    return request.app.state.token_metrics


def get_token_budget_gate(request: Request) -> GlobalTokenBudgetGate:
    """从应用状态获取全局金额预算闸门。"""
    return request.app.state.token_budget_gate


def get_faithfulness_metrics(request: Request) -> FaithfulnessMetrics:
    """从应用状态获取单例忠实性评估指标记录器。"""
    return request.app.state.faithfulness_metrics


def get_query_cache_service(
    settings: Settings = Depends(get_settings),
) -> QueryCacheService:
    """构建普通 RAG 查询结果缓存服务。"""
    return QueryCacheService(
        get_redis(),
        ttl_seconds=settings.query_cache_ttl_seconds,
        timeout_seconds=settings.query_cache_timeout_seconds,
        max_retries=settings.query_cache_max_retries,
    )


def get_rag_query_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token_metrics: TokenMetrics = Depends(get_token_metrics),
    faithfulness_metrics: FaithfulnessMetrics = Depends(get_faithfulness_metrics),
    permission_service: PermissionService = Depends(get_permission_service),
) -> RagQueryPipeline:
    """根据配置构建 RAG 查询服务及其依赖。

    Args:
        session: 当前请求注入的异步数据库会话。
        settings: 应用配置，用于检索 TopK、上下文预算和模型参数。
        permission_service: 当前请求使用的知识库读权限服务。

    Returns:
        已组装依赖的查询管道。`v1` 为基础向量 RAG，`v2` 为混合检索 RAG，
        `v3` 为 HyDE 增强 RAG，`v4` 为 HyDE 增强 + Reranker 精排 RAG。
    """
    embedding_config = EmbeddingConfig(
        dimension=settings.embedding_dimension,
        batch_size=settings.embedding_batch_size,
        cache_version=settings.embedding_cache_version,
        cache_ttl_seconds=settings.embedding_cache_ttl_seconds,
        max_retries=settings.embedding_max_retries,
    )
    chunk_repository = ChunkRepository(session)
    embedding_service = EmbeddingService(
        get_embeddings(),
        get_redis(),
        embedding_config,
        token_metrics=token_metrics,
        model_name=getattr(settings, "embedding_model", "unknown"),
    )
    source_builder = SourceBuilder(max_context_chars=settings.rag_context_max_tokens * 4)
    chat_model = get_chat_model()

    if settings.rag_query_pipeline == "v1":
        return RagQueryService(
            embedding_service=embedding_service,
            chunk_repository=chunk_repository,
            source_builder=source_builder,
            chat_model=chat_model,
            token_metrics=token_metrics,
            settings=settings,
        )

    hybrid_retriever = HybridRetriever(
        embedding_service=embedding_service,
        chunk_repository=chunk_repository,
        ts_query_builder=TsQueryBuilder(),
        settings=settings,
        permission_service=permission_service,
    )

    if settings.rag_query_pipeline == "v2":
        return RagQueryServiceV2(
            retriever=hybrid_retriever,
            source_builder=source_builder,
            chat_model=chat_model,
            token_metrics=token_metrics,
            settings=settings,
        )

    if settings.rag_query_pipeline in {"v3", "v4"}:
        enhanced_retriever = EnhancedRetriever(
            query_rewriter=QueryRewriter(
                chat_model=chat_model,
                redis_client=get_redis(),
                chat_model_name=settings.chat_model,
                cache_ttl_seconds=settings.query_cache_ttl_seconds,
                token_metrics=token_metrics,
            ),
            hybrid_retriever=hybrid_retriever,
            embedding_service=embedding_service,
            chunk_repository=chunk_repository,
            rrf_k=settings.rag_rrf_k,
            hyde_vector_top_k=settings.rag_vector_top_k,
        )

    if settings.rag_query_pipeline == "v3":
        return RagQueryServiceV3(
            retriever=enhanced_retriever,
            source_builder=source_builder,
            chat_model=chat_model,
            token_metrics=token_metrics,
            settings=settings,
        )

    if settings.rag_query_pipeline == "v4":
        return RagQueryServiceV4(
            retriever=enhanced_retriever,
            reranker=RerankerService(
                client=DashScopeRerankerClient(settings=settings),
                top_n=settings.reranker_top_n,
            ),
            confidence_filter=ConfidenceFilter(min_score=settings.rag_min_score),
            context_trimmer=ContextTrimmer(
                max_context_tokens=settings.rag_context_max_tokens,
                token_metrics=token_metrics,
            ),
            source_builder=source_builder,
            chat_model=chat_model,
            token_metrics=token_metrics,
            settings=settings,
            faithfulness_evaluator=FaithfulnessEvaluator(
                chat_model=chat_model,
                token_metrics=token_metrics,
                sampling_rate=settings.rag_faithfulness_sample_rate,
                timeout_seconds=settings.rag_faithfulness_timeout_seconds,
                metrics=faithfulness_metrics,
                model_name=settings.chat_model,
            ),
        )

    raise ValueError("rag_query_pipeline must be 'v1', 'v2', 'v3' or 'v4'")


@router.post("/query")
async def query_rag(
    request: RagQueryRequest,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    rag_service: RagQueryPipeline = Depends(get_rag_query_service),
    query_cache: QueryCacheService = Depends(get_query_cache_service),
    token_budget_gate: GlobalTokenBudgetGate = Depends(get_token_budget_gate),
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
    started_at = time.perf_counter()

    if not request.kb_ids:
        raise HTTPException(status_code=422, detail="kb_ids must not be empty")

    # 读权限必须在缓存读取、向量化和检索之前完成，避免无权限范围泄露缓存命中状态。
    for kb_id in request.kb_ids:
        await permission_service.require_read(kb_id, user)

    cached = await query_cache.get(request.question, request.kb_ids)
    if cached is not None:
        response = RagQueryResponse(
            answer=cached.answer,
            sources=cached.sources,
            hit_count=cached.hit_count,
            latency_ms=_elapsed_ms(started_at),
        )
        return ApiResponse.ok(response)

    try:
        await token_budget_gate.ensure_available()
    except TokenBudgetExhaustedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="今日金额预算已用尽",
        ) from exc
    except TokenBudgetUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="金额预算状态暂不可用",
        ) from exc

    async with token_budget_gate.request_scope():
        response = await rag_service.query(
            question=request.question,
            kb_ids=request.kb_ids,
            user=user,
        )
    await query_cache.put(request.question, request.kb_ids, response)
    return ApiResponse.ok(response.model_copy(update={"latency_ms": _elapsed_ms(started_at)}))


def _elapsed_ms(started_at: float) -> int:
    """计算普通 RAG 业务入口的完整服务耗时。"""
    return max(0, int((time.perf_counter() - started_at) * 1000))
