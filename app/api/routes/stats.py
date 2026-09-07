from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.dependencies import get_current_user
from app.core.context import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.stats import TokenStatsResponse
from app.services.token_cost import TokenCostService
from app.services.token_metrics import TokenMetrics, TokenMetricsUnavailableError

router = APIRouter()


def get_token_metrics(request: Request) -> TokenMetrics:
    """从应用状态获取 Token 统计记录器。"""
    return request.app.state.token_metrics


def get_token_cost_service(
    token_metrics: TokenMetrics = Depends(get_token_metrics),
) -> TokenCostService:
    """构建读取 Redis 已累计金额的当前用户成本服务。"""
    return TokenCostService(token_metrics=token_metrics)


@router.get("/tokens", response_model=ApiResponse[TokenStatsResponse])
async def get_token_stats(
    user: CurrentUser = Depends(get_current_user),
    cost_service: TokenCostService = Depends(get_token_cost_service),
) -> ApiResponse[TokenStatsResponse]:
    """返回当前认证用户的累计 Token 与人民币成本估算。"""
    try:
        summary = await cost_service.get_user_cost(user_id=user.user_id)
    except TokenMetricsUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Token 统计暂不可用",
        ) from exc

    return ApiResponse.ok(
        TokenStatsResponse(
            embedding_tokens=summary.embedding_tokens,
            input_tokens=summary.input_tokens,
            answer_generation_tokens=summary.answer_generation_tokens,
            intent_tokens=summary.intent_tokens,
            hyde_tokens=summary.hyde_tokens,
            reranker_tokens=summary.reranker_tokens,
            faithfulness_tokens=summary.faithfulness_tokens,
            estimated_cost=f"{summary.estimated_cost:.4f}",
            embedding_cost=f"{summary.embedding_cost:.4f}",
            input_cost=f"{summary.input_cost:.4f}",
            answer_generation_cost=f"{summary.answer_generation_cost:.4f}",
            intent_cost=f"{summary.intent_cost:.4f}",
            hyde_cost=f"{summary.hyde_cost:.4f}",
            reranker_cost=f"{summary.reranker_cost:.4f}",
            faithfulness_cost=f"{summary.faithfulness_cost:.4f}",
        )
    )
