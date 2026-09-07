from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.token_metrics import UserTokenUsage
from app.services.token_cost import TokenCostService


class FakeTokenMetrics:
    def __init__(self, usage: UserTokenUsage | None = None) -> None:
        self.usage = usage or UserTokenUsage(
            embedding_tokens=125_000,
            input_tokens=890_000,
            answer_generation_tokens=210_000,
            intent_tokens=0,
            hyde_tokens=0,
            reranker_tokens=0,
            faithfulness_tokens=0,
            estimated_cost_cny=Decimal("1.2195"),
        )

    async def read_user_tokens(self, user_id: int) -> UserTokenUsage:
        assert user_id == 7
        return self.usage


@pytest.mark.asyncio
async def test_token_cost_service_calculates_fixed_decimal_estimate() -> None:
    service = TokenCostService(
        token_metrics=FakeTokenMetrics(),
    )

    summary = await service.get_user_cost(user_id=7)

    assert summary.embedding_tokens == 125_000
    assert summary.input_tokens == 890_000
    assert summary.answer_generation_tokens == 210_000
    assert summary.intent_tokens == 0
    assert summary.hyde_tokens == 0
    assert summary.reranker_tokens == 0
    assert summary.faithfulness_tokens == 0
    assert summary.estimated_cost == Decimal("1.2195")


@pytest.mark.asyncio
async def test_token_cost_service_rounds_half_up_to_four_decimal_places() -> None:
    service = TokenCostService(
        token_metrics=FakeTokenMetrics(
                UserTokenUsage(
                    embedding_tokens=1_000,
                    input_tokens=0,
                    answer_generation_tokens=0,
                    intent_tokens=0,
                    hyde_tokens=0,
                    reranker_tokens=0,
                    faithfulness_tokens=0,
                    estimated_cost_cny=Decimal("0.00005"),
                )
        ),
    )

    summary = await service.get_user_cost(user_id=7)

    assert summary.estimated_cost == Decimal("0.0001")


@pytest.mark.asyncio
async def test_token_cost_service_prices_internal_outputs_and_reranker() -> None:
    service = TokenCostService(
        token_metrics=FakeTokenMetrics(
            UserTokenUsage(
                embedding_tokens=0,
                input_tokens=1_000,
                answer_generation_tokens=1_000,
                intent_tokens=1_000,
                hyde_tokens=1_000,
                reranker_tokens=1_000,
                faithfulness_tokens=1_000,
                estimated_cost_cny=Decimal("0.0075"),
            )
        ),
    )

    summary = await service.get_user_cost(user_id=7)

    assert summary.estimated_cost == Decimal("0.0075")
