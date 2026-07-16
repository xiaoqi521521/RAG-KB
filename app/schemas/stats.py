from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TokenStatsResponse(BaseModel):
    """当前用户 Token 用量和人民币近似成本响应。"""

    embedding_tokens: int = Field(ge=0)
    context_tokens: int = Field(ge=0)
    generation_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost: str
    currency: Literal["CNY"] = "CNY"
