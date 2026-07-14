from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings

ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    settings = get_settings()
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> int:
    """验证 JWT 并返回其中的用户标识。

    Args:
        token: HTTP Bearer Token 中携带的 JWT。

    Returns:
        已验证的正整数用户 ID。

    Raises:
        ValueError: Token 无效、过期或缺少合法用户标识时抛出。
    """
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
        subject = payload.get("sub")
        user_id = int(subject)
    except (JWTError, TypeError, ValueError) as exc:
        raise ValueError("无效的访问令牌") from exc

    if user_id <= 0:
        raise ValueError("无效的访问令牌")
    return user_id


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)
