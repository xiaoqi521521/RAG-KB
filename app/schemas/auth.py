from pydantic import BaseModel


class LoginRequest(BaseModel):
    """登录接口请求参数。"""

    username: str
    password: str
