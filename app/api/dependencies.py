from collections.abc import AsyncIterator
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import get_settings
from app.core.context import CurrentUser, current_user_var
from app.core.security import decode_access_token
from app.services.identity import (
    IdentityProvider,
    IdentityProviderUnavailableError,
    demo_identity_provider,
)
from app.services.token_metrics import TokenUsageRecorder

settings = get_settings()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_prefix}/auth/login")
logger = logging.getLogger(__name__)


def get_app_token_metrics(request: Request) -> TokenUsageRecorder:
    """从应用生命周期状态获取统一 Token recorder。"""
    return request.app.state.token_metrics


def get_identity_provider() -> IdentityProvider:
    """提供当前环境的身份目录适配器。

    Returns:
        用于读取用户资料的身份提供者。生产环境需要替换为实际用户目录。
    """
    return demo_identity_provider


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    identity_provider: IdentityProvider = Depends(get_identity_provider),
) -> AsyncIterator[CurrentUser]:
    """验证 JWT 并加载请求内的当前用户。

    Args:
        token: OAuth2 Bearer 依赖提取的 JWT。
        identity_provider: 实时读取部门、角色和启用状态的用户目录。

    Yields:
        当前请求可用的用户身份。
    """
    try:
        user_id = decode_access_token(token)
    except ValueError as exc:
        logger.info("authentication_failed=true reason=invalid_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的访问令牌",
        ) from exc

    try:
        user = await identity_provider.load_current_user(user_id)
    except IdentityProviderUnavailableError as exc:
        logger.warning("authentication_failed=true reason=identity_provider_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="身份服务暂不可用",
        ) from exc

    if user is None:
        logger.info("authentication_failed=true reason=inactive_or_missing_user")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已停用",
        )

    token_var = current_user_var.set(user)
    try:
        yield user
    finally:
        current_user_var.reset(token_var)
