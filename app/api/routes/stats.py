from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.dependencies import get_current_user
from app.core.config import Settings, get_settings
from app.core.context import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.stats import DailyUsageResponse, TokenStatsResponse
from app.services.token_cost import TokenCostService
from app.services.token_metrics import TokenMetrics, TokenMetricsUnavailableError
from app.services.usage_history import UsageHistoryError, UsageHistoryService

router = APIRouter()


def get_token_metrics(request: Request) -> TokenMetrics:
    """从应用状态获取 Token 统计记录器。"""
    return request.app.state.token_metrics


def get_token_cost_service(
    token_metrics: TokenMetrics = Depends(get_token_metrics),
) -> TokenCostService:
    """构建读取 Redis 已累计金额的当前用户成本服务。"""
    return TokenCostService(token_metrics=token_metrics)


def get_usage_history_service(
    settings: Settings = Depends(get_settings),
) -> UsageHistoryService:
    """构建查询 Prometheus 每日用量历史的服务。"""
    return UsageHistoryService(
        base_url=settings.prometheus_base_url,
        timeout_seconds=settings.prometheus_query_timeout_seconds,
        timezone=settings.token_budget_timezone,
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


@router.get("/usage/daily", response_model=ApiResponse[list[DailyUsageResponse]])
async def get_daily_usage_history(
    days: int = Query(default=14, ge=1, le=90),
    user: CurrentUser = Depends(get_current_user),
    usage_history: UsageHistoryService = Depends(get_usage_history_service),
) -> ApiResponse[list[DailyUsageResponse]]:
    """返回全系统每日 Token 总量与估算成本，仅系统管理员可查看。"""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅系统管理员可查看全局用量",
        )
    try:
        points = await usage_history.daily_usage(days=days)
    except UsageHistoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="用量历史暂不可用",
        ) from exc
    return ApiResponse.ok(
        [
            DailyUsageResponse(date=point.date, tokens=point.tokens, cost=f"{point.cost:.4f}")
            for point in points
        ]
    )


@router.get("/usage/access", response_model=ApiResponse[bool])
async def get_usage_access(
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse[bool]:
    """轻量权限探测，仅判断系统管理员身份，不触发 Prometheus 查询。"""
    return ApiResponse.ok(user.is_admin)
