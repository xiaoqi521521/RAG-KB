from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app.core.context import CurrentUser, get_current_user_from_context


class FakeIdentityProvider:
    """提供可控的当前用户资料，模拟身份目录边界。"""

    def __init__(self, user: CurrentUser | None = None, *, unavailable: bool = False) -> None:
        self.user = user
        self.unavailable = unavailable
        self.calls: list[int] = []

    async def load_current_user(self, user_id: int) -> CurrentUser | None:
        self.calls.append(user_id)
        if self.unavailable:
            from app.services.identity import IdentityProviderUnavailableError

            raise IdentityProviderUnavailableError("身份目录不可用")
        return self.user


def _access_token(user_id: int) -> str:
    return jwt.encode({"sub": str(user_id)}, "test-secret", algorithm="HS256")


def _client(monkeypatch: pytest.MonkeyPatch, provider: FakeIdentityProvider) -> TestClient:
    from app.api import dependencies
    from app.core import security

    monkeypatch.setattr(security, "get_settings", lambda: SimpleNamespace(secret_key="test-secret"))

    app = FastAPI()

    @app.get("/me")
    async def current_user(user: CurrentUser = Depends(dependencies.get_current_user)) -> dict[str, object]:
        assert get_current_user_from_context() == user
        return {"user_id": user.user_id, "department_id": user.department_id, "role": user.role}

    app.dependency_overrides[dependencies.get_identity_provider] = lambda: provider
    return TestClient(app)


def test_current_user_uses_jwt_subject_and_loads_identity_for_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeIdentityProvider(CurrentUser(user_id=7, department_id="engineering", role="MEMBER"))

    with _client(monkeypatch, provider) as client:
        headers = {"Authorization": f"Bearer {_access_token(7)}"}
        first = client.get("/me", headers=headers)
        second = client.get("/me", headers=headers)

    assert first.status_code == 200
    assert first.json() == {"user_id": 7, "department_id": "engineering", "role": "MEMBER"}
    assert second.status_code == 200
    assert provider.calls == [7, 7]
    with pytest.raises(RuntimeError, match="CurrentUser is not initialized"):
        get_current_user_from_context()


def test_current_user_rejects_missing_or_invalid_jwt_before_identity_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeIdentityProvider(CurrentUser(user_id=7, department_id="engineering", role="MEMBER"))

    with _client(monkeypatch, provider) as client:
        missing = client.get("/me")
        invalid = client.get("/me", headers={"Authorization": "Bearer invalid-token"})

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert provider.calls == []


def test_current_user_rejects_unknown_or_disabled_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeIdentityProvider()

    with _client(monkeypatch, provider) as client:
        response = client.get("/me", headers={"Authorization": f"Bearer {_access_token(7)}"})

    assert response.status_code == 401
    assert provider.calls == [7]


def test_current_user_returns_service_unavailable_when_identity_provider_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeIdentityProvider(unavailable=True)

    with _client(monkeypatch, provider) as client:
        response = client.get("/me", headers={"Authorization": f"Bearer {_access_token(7)}"})

    assert response.status_code == 503
    assert provider.calls == [7]
