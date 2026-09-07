from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Protocol

from app.services.token_metrics import UserTokenUsage

_CURRENCY_QUANTUM = Decimal("0.0001")


class UserTokenReader(Protocol):
    async def read_user_tokens(self, user_id: int) -> UserTokenUsage: ...


@dataclass(frozen=True)
class TokenCostSummary:
    """当前用户七类 Token 用量、分类型近似成本及总成本。"""

    embedding_tokens: int
    input_tokens: int
    answer_generation_tokens: int
    intent_tokens: int
    hyde_tokens: int
    reranker_tokens: int
    faithfulness_tokens: int
    estimated_cost: Decimal
    embedding_cost: Decimal
    input_cost: Decimal
    answer_generation_cost: Decimal
    intent_cost: Decimal
    hyde_cost: Decimal
    reranker_cost: Decimal
    faithfulness_cost: Decimal


class TokenCostService:
    """读取用户 Token 累计值和已持久化的金额。"""

    def __init__(
        self,
        *,
        token_metrics: UserTokenReader,
    ) -> None:
        self.token_metrics = token_metrics

    async def get_user_cost(self, *, user_id: int) -> TokenCostSummary:
        """返回当前用户的六类 Token 和 Redis 已累计的四位小数成本。"""
        usage = await self.token_metrics.read_user_tokens(user_id)
        return TokenCostSummary(
            embedding_tokens=usage.embedding_tokens,
            input_tokens=usage.input_tokens,
            answer_generation_tokens=usage.answer_generation_tokens,
            intent_tokens=usage.intent_tokens,
            hyde_tokens=usage.hyde_tokens,
            reranker_tokens=usage.reranker_tokens,
            faithfulness_tokens=usage.faithfulness_tokens,
            estimated_cost=usage.estimated_cost_cny.quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_HALF_UP,
            ),
            embedding_cost=usage.embedding_cost_cny.quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_HALF_UP,
            ),
            input_cost=usage.input_cost_cny.quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_HALF_UP,
            ),
            answer_generation_cost=usage.answer_generation_cost_cny.quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_HALF_UP,
            ),
            intent_cost=usage.intent_cost_cny.quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_HALF_UP,
            ),
            hyde_cost=usage.hyde_cost_cny.quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_HALF_UP,
            ),
            reranker_cost=usage.reranker_cost_cny.quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_HALF_UP,
            ),
            faithfulness_cost=usage.faithfulness_cost_cny.quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_HALF_UP,
            ),
        )
