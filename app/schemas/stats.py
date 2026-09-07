from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TokenStatsResponse(BaseModel):
    """当前用户七类 Token 用量和人民币近似成本响应。"""

    embedding_tokens: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    answer_generation_tokens: int = Field(ge=0)
    intent_tokens: int = Field(ge=0)
    hyde_tokens: int = Field(ge=0)
    reranker_tokens: int = Field(ge=0)
    faithfulness_tokens: int = Field(ge=0)
    estimated_cost: str
    embedding_cost: str
    input_cost: str
    answer_generation_cost: str
    intent_cost: str
    hyde_cost: str
    reranker_cost: str
    faithfulness_cost: str
    currency: Literal["CNY"] = "CNY"
