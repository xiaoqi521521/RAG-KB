import logging
from contextlib import asynccontextmanager
from typing import Any, cast

import uvicorn
from fastapi import Depends, FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.router import api_router
from app.core.clients import close_clients, init_clients
from app.core.clients import get_redis
from app.core.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.telemetry import init_metrics, shutdown_metrics
from app.core.trace_id import register_trace_id_middleware
from app.core.database import get_db
from app.repositories.evaluations import EvaluationRepository
from app.services.token_budget import GlobalTokenBudgetGate
from app.services.daily_usage_metrics import DailyUsageMetrics
from app.services.token_metrics import TokenMetrics
from app.services.usage_history import UsageHistoryService
from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    await init_clients(settings)
    meter_provider = init_metrics(settings)
    app.state.meter_provider = meter_provider
    # lifespan 内注册全局 provider 后再取 meter，保证指标落到 Prometheus reader。
    meter = meter_provider.get_meter("rag-kb.token-metrics") if meter_provider is not None else None
    token_budget_gate = GlobalTokenBudgetGate(
        redis_client=cast(Any, get_redis()),
        daily_budget_cny=settings.token_budget_daily_cny,
        timezone=settings.token_budget_timezone,
        request_cost_limit_cny=settings.token_request_alert_cost_cny,
        timeout_seconds=settings.token_stats_timeout_seconds,
        meter=meter,
    )
    app.state.token_budget_gate = token_budget_gate
    # 启动时预热当日预算 key：该 key 原本在首个预算请求时才创建，部署或重启后
    # Redis 中会缺失全局预算记录，80% 预警等监控也无从谈起。
    try:
        await token_budget_gate.ensure_available()
    except Exception as exc:  # noqa: BLE001
        # 预热失败不阻断启动；每个请求仍会逐次检查，Redis 恢复后自动自愈。
        logger.warning("token budget warmup failed: error_type=%s", type(exc).__name__)
    app.state.token_metrics = TokenMetrics(
        redis_client=cast(Any, get_redis()),
        meter=meter,
        write_failure_counter=token_budget_gate.write_failure_counter,
        read_timeout_seconds=settings.token_stats_timeout_seconds,
        read_max_retries=settings.token_stats_max_retries,
        budget_gate=token_budget_gate,
        embedding_price=settings.embedding_input_cost_cny_per_1k_tokens,
        chat_input_price=settings.chat_input_cost_cny_per_1k_tokens,
        chat_output_price=settings.chat_output_cost_cny_per_1k_tokens,
        reranker_price=settings.reranker_cost_cny_per_1k_tokens,
        timezone=settings.token_budget_timezone,
    )
    app.state.daily_usage_metrics = (
        DailyUsageMetrics(meter=meter) if meter_provider is not None else None
    )
    try:
        yield
    finally:
        shutdown_metrics(meter_provider)
        await close_clients()


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。

    Returns:
        已配置路由、异常处理器、生命周期钩子和监控指标的 FastAPI 应用。
    """
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        # APP_DEBUG 仅控制本地热重载，HTTP 异常始终使用不泄露内部细节的统一响应。
        debug=False,
        lifespan=lifespan,
    )
    register_trace_id_middleware(app)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    if settings.enable_metrics:
        # 普通路由直接输出 exposition，避免 Starlette Mount 的尾斜杠 307 重定向。
        @app.get("/metrics", include_in_schema=False)
        async def metrics(
            session: AsyncSession = Depends(get_db),
        ) -> Response:
            usage_history = UsageHistoryService(
                daily_usage_store=app.state.token_metrics,
                timezone=settings.token_budget_timezone,
                run_usage_repository=EvaluationRepository(session),
            )
            try:
                point = (await usage_history.daily_usage(days=1))[0]
                app.state.daily_usage_metrics.update(
                    tokens=point.tokens,
                    cost_cny=point.cost,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "daily usage metric refresh failed: error_type=%s",
                    type(exc).__name__,
                )
            return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
    return app


def start() -> None:
    """启动本地 API 服务。

    Returns:
        None。该方法会阻塞当前进程并交由 Uvicorn 托管 ASGI 应用。
    """
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=settings.debug,
        limit_concurrency=settings.server_limit_concurrency,
        timeout_keep_alive=settings.server_timeout_keep_alive_seconds,
        timeout_graceful_shutdown=settings.server_timeout_graceful_shutdown_seconds,
        log_level=settings.log_level.lower(),
    )


app = create_app()


if __name__ == "__main__":
    start()
