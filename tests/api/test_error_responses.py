from __future__ import annotations

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.schemas.common import ApiResponse


class DemoRequest(BaseModel):
    """用于在测试中触发 FastAPI 参数校验错误的请求体。"""

    name: str


def test_api_response_error_builds_standard_error_payload() -> None:
    """验证 ApiResponse.error 返回统一响应信封结构。"""
    response = ApiResponse.error(code=404, message="文档不存在")

    assert response.code == 404
    assert response.message == "文档不存在"
    assert response.data is None


def test_http_exception_uses_standard_error_payload() -> None:
    """验证 HTTPException 会被包装为统一 API 响应信封。"""
    from app.core.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/missing")
    async def missing() -> None:
        """抛出 HTTPException，验证全局异常处理器会格式化响应。"""
        raise HTTPException(status_code=404, detail="文档不存在")

    with TestClient(app) as client:
        response = client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "文档不存在", "data": None}


def test_request_validation_error_uses_standard_error_payload() -> None:
    """验证请求参数校验错误会被包装为统一 API 响应信封。"""
    from app.core.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/demo")
    async def demo(request: DemoRequest) -> dict[str, str]:
        """返回已校验请求，用无效请求体触发 FastAPI 参数校验。"""
        return {"name": request.name}

    with TestClient(app) as client:
        response = client.post("/demo", json={})

    payload = response.json()
    assert response.status_code == 422
    assert payload["code"] == 422
    assert payload["message"] == "请求参数校验失败"
    assert payload["data"] is None
