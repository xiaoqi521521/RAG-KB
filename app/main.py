from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.router import api_router
from app.core.clients import close_clients, init_clients
from app.core.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    await init_clients(settings)
    yield
    await close_clients()


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。

    Returns:
        已配置路由、异常处理器、生命周期钩子和监控指标的 FastAPI 应用。
    """
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
    )
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
