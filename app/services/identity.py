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
