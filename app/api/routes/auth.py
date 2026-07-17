import logging

from fastapi import APIRouter, HTTPException, status

from app.core.security import create_access_token
from app.schemas.auth import LoginRequest
from app.schemas.common import ApiResponse
from app.services.identity import demo_identity_provider

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/login", response_model=ApiResponse[str])
async def login(request: LoginRequest) -> ApiResponse[str]:
    """校验演示账号并签发仅包含用户标识的访问令牌。"""
    user = await demo_identity_provider.authenticate(request.username, request.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token = create_access_token(data={"sub": str(user.user_id)})
    logger.info(
        "login_succeeded=true username=%s user_id=%s department_id=%s",
        request.username,
        user.user_id,
        user.department_id,
    )
    return ApiResponse.ok(token)
