from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.common import ApiResponse

logger = logging.getLogger(__name__)


def _response_content(code: int, message: str) -> dict[str, Any]:
    """为 JSONResponse 序列化统一错误响应。

    Args:
        code: 暴露在 API 响应体中的错误码。
        message: 暴露在 API 响应体中的可读错误消息。

    Returns:
        与 ApiResponse 信封一致的可 JSON 序列化字典。
    """
    return ApiResponse.error(code=code, message=message).model_dump(mode="json")


def _message_from_http_detail(detail: Any) -> str:
    """将 HTTPException 的 detail 转换为稳定的客户端错误消息。

    Args:
        detail: FastAPI 或 Starlette HTTPException 携带的 detail 字段。

    Returns:
        detail 为字符串时返回原文，否则返回通用请求失败消息。
    """
    if isinstance(detail, str):
        return detail
    return "请求处理失败"


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """使用统一 API 响应信封处理 HTTP 错误。

    Args:
        request: FastAPI 传入的请求对象。
        exc: 应用代码或 Starlette 内部抛出的 HTTP 异常。

    Returns:
        包含 code、message、data 字段的 JSONResponse。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=_response_content(exc.status_code, _message_from_http_detail(exc.detail)),
        headers=getattr(exc, "headers", None),
    )


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """使用统一 API 响应信封处理请求参数校验错误。

    Args:
        request: FastAPI 传入的请求对象。
        exc: 路由处理函数执行前抛出的参数校验异常。

    Returns:
        包含标准参数校验失败消息的 JSONResponse。
    """
    return JSONResponse(
        status_code=422,
        content=_response_content(422, "请求参数校验失败"),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理未预期错误，并避免泄露内部异常细节。

    Args:
        request: FastAPI 传入的请求对象。
        exc: 请求处理过程中抛出的未捕获异常。

    Returns:
        包含通用服务器内部错误消息的 JSONResponse。
    """
    logger.exception("未处理的请求异常：method=%s url=%s", request.method, request.url)
    return JSONResponse(
        status_code=500,
        content=_response_content(500, "服务器内部错误"),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """为 FastAPI 应用注册全局异常处理器。

    Args:
        app: 需要使用统一错误响应的 FastAPI 应用实例。

    Returns:
        None。该函数会在传入应用上注册异常处理器。
    """
    # 注册 Starlette 的 HTTPException，确保 FastAPI 抛出的异常和
    # Starlette 内部 HTTP 错误使用同一套响应信封。
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
