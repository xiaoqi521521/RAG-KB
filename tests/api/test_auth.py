from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.context import CurrentUser


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app.api import dependencies
    from app.api.router import api_router
    from app.core import security
    from app.core.exception_handlers import register_exception_handlers

    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(secret_key="test-secret", access_token_expire_minutes=30),
    )

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/me")
    async def current_user(
        user: CurrentUser = Depends(dependencies.get_current_user),
    ) -> dict[str, object]:
        return {
            "user_id": user.user_id,
            "department_id": user.department_id,
            "role": user.role,
        }

    return TestClient(app)


def test_login_returns_access_token_for_demo_user(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "hr001", "password": "demo123"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["message"] == "success"
    assert isinstance(body["data"], str)
    assert body["data"]


def test_login_rejects_invalid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "hr001", "password": "incorrect"},
        )

    assert response.status_code == 401
    assert response.json() == {
        "code": 401,
        "message": "用户名或密码错误",
        "data": None,
    }


def test_login_token_resolves_demo_identity_on_protected_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        login_response = client.post(
            "/api/v1/auth/login",
            json={"username": "hr001", "password": "demo123"},
        )
        response = client.get(
            "/me",
            headers={"Authorization": f"Bearer {login_response.json()['data']}"},
        )

    assert response.status_code == 200
    assert response.json() == {"user_id": 1, "department_id": "HR", "role": "MEMBER"}
