import asyncio
import logging
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exception_handlers import register_exception_handlers
from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.trace_id import register_trace_id_middleware

logger = logging.getLogger("tests.trace_id")


def _configure_test_logging() -> None:
    """使用最小配置对象启用测试所需的全局日志格式。"""
    settings = cast(Settings, SimpleNamespace(log_level="INFO"))
    configure_logging(settings)


def _build_app() -> FastAPI:
    """构造仅包含 Trace ID 中间件的测试应用。"""
    app = FastAPI()
    register_trace_id_middleware(app)
    register_exception_handlers(app)

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("不得出现在响应中的内部错误")

    @app.get("/log")
    async def log_request() -> dict[str, bool]:
        logger.info("request-log")
        return {"ok": True}

    concurrent_requests = 0
    release_concurrent_requests = asyncio.Event()

    @app.get("/concurrent/{marker}")
    async def concurrent_request(marker: str) -> dict[str, str]:
        nonlocal concurrent_requests
        concurrent_requests += 1
        if concurrent_requests == 2:
            release_concurrent_requests.set()
        await release_concurrent_requests.wait()
        logger.info("concurrent-log-%s", marker)
        return {"marker": marker}

    return app


def test_safe_client_trace_id_is_returned_unchanged() -> None:
    """合法客户端 Trace ID 应贯穿请求并原样写回响应。"""
    with TestClient(_build_app()) as client:
        response = client.get("/ok", headers={"X-Trace-Id": "client.trace-123_ABC"})

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "client.trace-123_ABC"


@pytest.mark.parametrize("unsafe_trace_id", ["", "contains space", "contains/slash", "a" * 65])
def test_missing_or_unsafe_client_trace_id_is_replaced(unsafe_trace_id: str) -> None:
    """缺失或含不安全字符的值必须替换为服务端 UUID。"""
    headers = {"X-Trace-Id": unsafe_trace_id} if unsafe_trace_id else {}

    with TestClient(_build_app()) as client:
        response = client.get("/ok", headers=headers)

    generated_trace_id = response.headers["X-Trace-Id"]
    assert UUID(generated_trace_id).version == 4
    assert generated_trace_id != unsafe_trace_id


def test_framework_error_response_retains_trace_id() -> None:
    """路由不存在等框架错误也应返回请求 Trace ID。"""
    with TestClient(_build_app()) as client:
        response = client.get("/missing", headers={"X-Trace-Id": "missing-route-123"})

    assert response.status_code == 404
    assert response.headers["X-Trace-Id"] == "missing-route-123"


def test_unhandled_error_response_retains_trace_id_without_leaking_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未处理异常也应返回 Trace ID，并继续使用通用错误响应。"""
    _configure_test_logging()
    with caplog.at_level(logging.ERROR, logger="app.core.exception_handlers"):
        with TestClient(_build_app(), raise_server_exceptions=False) as client:
            response = client.get("/crash", headers={"X-Trace-Id": "error-trace-123"})
        logger.error("after-error-log")

    assert response.status_code == 500
    assert response.headers["X-Trace-Id"] == "error-trace-123"
    assert response.json() == {"code": 500, "message": "服务器内部错误", "data": None}
    records = {record.message: record for record in caplog.records}
    error_record = next(
        record for record in caplog.records if record.name == "app.core.exception_handlers"
    )
    assert getattr(error_record, "trace_id") == "error-trace-123"
    assert getattr(records["after-error-log"], "trace_id") == "-"


def test_request_logs_receive_trace_id_and_context_is_cleaned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """业务日志无需重复传参，请求完成后也不得残留 Trace ID。"""
    _configure_test_logging()

    with caplog.at_level(logging.INFO, logger="tests.trace_id"):
        with TestClient(_build_app()) as client:
            response = client.get("/log", headers={"X-Trace-Id": "log-trace-123"})
        logger.info("outside-log")

    assert response.status_code == 200
    records = {record.message: record for record in caplog.records}
    assert getattr(records["request-log"], "trace_id") == "log-trace-123"
    assert getattr(records["outside-log"], "trace_id") == "-"


@pytest.mark.asyncio
async def test_concurrent_requests_keep_trace_ids_isolated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """并发请求应在各自异步上下文中保留独立 Trace ID。"""
    _configure_test_logging()
    transport = httpx.ASGITransport(app=_build_app())

    with caplog.at_level(logging.INFO, logger="tests.trace_id"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first, second = await asyncio.gather(
                client.get("/concurrent/first", headers={"X-Trace-Id": "trace-first"}),
                client.get("/concurrent/second", headers={"X-Trace-Id": "trace-second"}),
            )

    assert first.headers["X-Trace-Id"] == "trace-first"
    assert second.headers["X-Trace-Id"] == "trace-second"
    records = {
        record.message: record
        for record in caplog.records
        if "concurrent-log" in record.message
    }
    assert getattr(records["concurrent-log-first"], "trace_id") == "trace-first"
    assert getattr(records["concurrent-log-second"], "trace_id") == "trace-second"
