from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from uuid import uuid4

from fastapi import FastAPI, Request, Response

TRACE_ID_HEADER = "X-Trace-Id"
_TRACE_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def get_trace_id() -> str | None:
    """读取当前请求的 Trace ID；请求外返回空值。"""
    return trace_id_var.get()


def register_trace_id_middleware(app: FastAPI) -> None:
    """注册请求 Trace ID 中间件。

    Args:
        app: 需要启用 Trace ID 传播的 FastAPI 应用。

    Returns:
        None。
    """

    @app.middleware("http")
    async def trace_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = _resolve_trace_id(request.headers.get(TRACE_ID_HEADER))
        request.state.trace_id = trace_id
        token = trace_id_var.set(trace_id)
        try:
            response = await call_next(request)
            response.headers[TRACE_ID_HEADER] = trace_id
            return response
        finally:
            trace_id_var.reset(token)


def _resolve_trace_id(raw_trace_id: str | None) -> str:
    """沿用安全的客户端值，否则生成不可注入日志的新 UUID。"""
    if raw_trace_id is not None and _TRACE_ID_PATTERN.fullmatch(raw_trace_id):
        return raw_trace_id
    return str(uuid4())
