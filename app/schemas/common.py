from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一 API 响应信封。

    Args:
        code: 返回给客户端的业务码或类 HTTP 状态码。
        message: 返回给客户端的可读响应消息。
        data: 成功或错误响应中携带的可选类型化数据。
    """

    code: int = 200
    message: str = "success"
    data: T | None = None

    @classmethod
    def ok(cls, data: T | None = None) -> "ApiResponse[T]":
        """构建成功 API 响应。

        Args:
            data: 可选响应数据。

        Returns:
            code 为 200、message 为 success，并携带指定 data 的 ApiResponse。
        """
        return cls(data=data)

    @classmethod
    def error(cls, code: int, message: str, data: T | None = None) -> "ApiResponse[T]":
        """构建错误 API 响应。

        Args:
            code: 错误码，通常与 HTTP 状态码保持一致。
            message: 返回给客户端的可读错误消息。
            data: 可选诊断数据；没有明确需要时保持 None。

        Returns:
            携带指定错误码、错误消息和可选数据的 ApiResponse。
        """
        return cls(code=code, message=message, data=data)
