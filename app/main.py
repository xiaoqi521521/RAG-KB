from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.router import api_router
from app.core.clients import close_clients, init_clients
from app.core.clients import get_redis
from app.core.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.telemetry import init_metrics, shutdown_metrics
from app.core.trace_id import register_trace_id_middleware
from app.services.faithfulness_evaluator import FaithfulnessMetrics
from app.services.token_metrics import TokenMetrics


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    await init_clients(settings)
    meter_provider = init_metrics(settings)
    meter = meter_provider.get_meter("rag-kb.token-metrics") if meter_provider is not None else None
    app.state.meter_provider = meter_provider
    app.state.token_metrics = TokenMetrics(redis_client=get_redis(), meter=meter)
    app.state.faithfulness_metrics = FaithfulnessMetrics(meter=meter)
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
        Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    return app


def start() -> None:
    """启动本地 API 服务。

    Returns:
        None。该方法会阻塞当前进程并交由 Uvicorn 托管 ASGI 应用。
    """
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=settings.debug,
        log_level="info",
    )


app = create_app()


if __name__ == "__main__":
    start()
