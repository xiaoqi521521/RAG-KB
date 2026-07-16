from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Protocol

from app.services.token_metrics import UserTokenUsage

_TOKEN_UNIT = Decimal("1000")
_CURRENCY_QUANTUM = Decimal("0.0001")


class UserTokenReader(Protocol):
    async def read_user_tokens(self, user_id: int) -> UserTokenUsage: ...


@dataclass(frozen=True)
class TokenCostSummary:
    """当前用户 Token 用量及人民币近似成本。"""

    embedding_tokens: int
    context_tokens: int
    generation_tokens: int
    total_tokens: int
    estimated_cost: Decimal


class TokenCostService:
    """读取用户 Token 累计值并计算可配置单价下的近似成本。"""

    def __init__(
        self,
        *,
        token_metrics: UserTokenReader,
        embedding_price: Decimal,
        chat_input_price: Decimal,
        chat_output_price: Decimal,
    ) -> None:
        self.token_metrics = token_metrics
        self.embedding_price = embedding_price
        self.chat_input_price = chat_input_price
        self.chat_output_price = chat_output_price

    async def get_user_cost(self, *, user_id: int) -> TokenCostSummary:
        """返回当前用户的三类 Token、总量和四位小数成本估算。"""
        usage = await self.token_metrics.read_user_tokens(user_id)
        total_tokens = usage.embedding_tokens + usage.context_tokens + usage.generation_tokens
        estimated_cost = self._estimate_cost(usage)
        return TokenCostSummary(
            embedding_tokens=usage.embedding_tokens,
            context_tokens=usage.context_tokens,
            generation_tokens=usage.generation_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
        )

    def _estimate_cost(self, usage: UserTokenUsage) -> Decimal:
        """按三类 Token 单价计算并四舍五入人民币金额。"""
        raw_cost = (
            Decimal(usage.embedding_tokens) / _TOKEN_UNIT * self.embedding_price
            + Decimal(usage.context_tokens) / _TOKEN_UNIT * self.chat_input_price
            + Decimal(usage.generation_tokens) / _TOKEN_UNIT * self.chat_output_price
        )
        return raw_cost.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
