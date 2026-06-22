from collections.abc import AsyncIterator

from fastapi import HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import get_settings
from app.core.context import CurrentUser, current_user_var

settings = get_settings()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_prefix}/auth/login")


async def get_current_user() -> AsyncIterator[CurrentUser]:
    user = CurrentUser(
        user_id=1,
        username="demo",
        tenant_id="default",
        department_id="default",
        role="ADMIN",
        allowed_kb_ids=(),
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    token_var = current_user_var.set(user)
    try:
        yield user
    finally:
        current_user_var.reset(token_var)
