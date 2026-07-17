from __future__ import annotations

from typing import Protocol

from app.core.context import CurrentUser


class IdentityProviderUnavailableError(Exception):
    """身份提供者暂不可用时抛出，调用方必须拒绝请求。"""


class IdentityProvider(Protocol):
    """按 JWT 中的用户标识加载当前用户资料。"""

    async def load_current_user(self, user_id: int) -> CurrentUser | None:
        """返回启用用户的当前资料，不存在或停用时返回 None。"""


class UnavailableIdentityProvider:
    """在尚未接入用户目录时显式拒绝受保护请求。"""

    async def load_current_user(self, user_id: int) -> CurrentUser | None:
        raise IdentityProviderUnavailableError("身份提供者不可用")


class DemoIdentityProvider:
    """为本地演示提供固定用户资料和登录校验。

    生产环境应替换为数据库、SSO 或 LDAP 身份目录；JWT 只保留用户 ID，
    请求中的部门和角色仍通过本提供者实时读取。
    """

    _profiles = {
        "hr001": CurrentUser(user_id=1, department_id="HR", role="MEMBER"),
        "tech001": CurrentUser(user_id=2, department_id="TECH", role="MEMBER"),
        "admin": CurrentUser(user_id=3, department_id="ALL", role="ADMIN"),
    }
    _password = "demo123"

    async def authenticate(self, username: str, password: str) -> CurrentUser | None:
        """校验演示账号并返回对应用户资料。"""
        if password != self._password:
            return None
        return self._profiles.get(username)

    async def load_current_user(self, user_id: int) -> CurrentUser | None:
        """按用户 ID 返回仍可用的演示用户资料。"""
        return next((user for user in self._profiles.values() if user.user_id == user_id), None)


demo_identity_provider = DemoIdentityProvider()
