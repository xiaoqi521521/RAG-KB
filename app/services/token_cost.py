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
    """当前用户六类 Token 用量及人民币近似成本。"""

    embedding_tokens: int
    input_tokens: int
    answer_generation_tokens: int
    hyde_tokens: int
    reranker_tokens: int
    faithfulness_tokens: int
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
        reranker_price: Decimal = Decimal("0"),
    ) -> None:
        self.token_metrics = token_metrics
        self.embedding_price = embedding_price
        self.chat_input_price = chat_input_price
        self.chat_output_price = chat_output_price
        self.reranker_price = reranker_price

    async def get_user_cost(self, *, user_id: int) -> TokenCostSummary:
        """返回当前用户的六类 Token、总量和四位小数成本估算。"""
        usage = await self.token_metrics.read_user_tokens(user_id)
        total_tokens = sum(
            (
                usage.embedding_tokens,
                usage.input_tokens,
                usage.answer_generation_tokens,
                usage.hyde_tokens,
                usage.reranker_tokens,
                usage.faithfulness_tokens,
            )
        )
        estimated_cost = self._estimate_cost(usage)
        return TokenCostSummary(
            embedding_tokens=usage.embedding_tokens,
            input_tokens=usage.input_tokens,
            answer_generation_tokens=usage.answer_generation_tokens,
            hyde_tokens=usage.hyde_tokens,
            reranker_tokens=usage.reranker_tokens,
            faithfulness_tokens=usage.faithfulness_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
        )

    def _estimate_cost(self, usage: UserTokenUsage) -> Decimal:
        """按六类 Token 单价计算并四舍五入人民币金额。"""
        raw_cost = (
            Decimal(usage.embedding_tokens) / _TOKEN_UNIT * self.embedding_price
            + Decimal(usage.input_tokens) / _TOKEN_UNIT * self.chat_input_price
            + Decimal(usage.answer_generation_tokens) / _TOKEN_UNIT * self.chat_output_price
            + Decimal(usage.hyde_tokens) / _TOKEN_UNIT * self.chat_output_price
            + Decimal(usage.faithfulness_tokens) / _TOKEN_UNIT * self.chat_output_price
            + Decimal(usage.reranker_tokens) / _TOKEN_UNIT * self.reranker_price
        )
        return raw_cost.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
