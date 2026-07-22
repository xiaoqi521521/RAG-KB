from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.dependencies import get_current_user
from app.core.config import Settings, get_settings
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
    settings: Settings = Depends(get_settings),
    token_metrics: TokenMetrics = Depends(get_token_metrics),
) -> TokenCostService:
    """按部署单价构建当前用户成本计算服务。"""
    return TokenCostService(
        token_metrics=token_metrics,
        embedding_price=settings.embedding_input_cost_cny_per_1k_tokens,
        chat_input_price=settings.chat_input_cost_cny_per_1k_tokens,
        chat_output_price=settings.chat_output_cost_cny_per_1k_tokens,
        reranker_price=settings.reranker_cost_cny_per_1k_tokens,
    )


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
            hyde_tokens=summary.hyde_tokens,
            reranker_tokens=summary.reranker_tokens,
            faithfulness_tokens=summary.faithfulness_tokens,
            total_tokens=summary.total_tokens,
            estimated_cost=f"{summary.estimated_cost:.4f}",
        )
    )
