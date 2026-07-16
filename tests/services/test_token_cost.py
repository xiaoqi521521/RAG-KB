from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.token_metrics import UserTokenUsage
from app.services.token_cost import TokenCostService


class FakeTokenMetrics:
    def __init__(self, usage: UserTokenUsage | None = None) -> None:
        self.usage = usage or UserTokenUsage(
            embedding_tokens=125_000,
            context_tokens=890_000,
            generation_tokens=210_000,
        )

    async def read_user_tokens(self, user_id: int) -> UserTokenUsage:
        assert user_id == 7
        return self.usage


@pytest.mark.asyncio
async def test_token_cost_service_calculates_fixed_decimal_estimate() -> None:
    service = TokenCostService(
        token_metrics=FakeTokenMetrics(),
        embedding_price=Decimal("0.0007"),
        chat_input_price=Decimal("0.0008"),
        chat_output_price=Decimal("0.002"),
    )

    summary = await service.get_user_cost(user_id=7)

    assert summary.embedding_tokens == 125_000
    assert summary.context_tokens == 890_000
    assert summary.generation_tokens == 210_000
    assert summary.total_tokens == 1_225_000
    assert summary.estimated_cost == Decimal("1.2195")


@pytest.mark.asyncio
async def test_token_cost_service_rounds_half_up_to_four_decimal_places() -> None:
    service = TokenCostService(
        token_metrics=FakeTokenMetrics(
            UserTokenUsage(embedding_tokens=1_000, context_tokens=0, generation_tokens=0)
        ),
        embedding_price=Decimal("0.00005"),
        chat_input_price=Decimal("0"),
        chat_output_price=Decimal("0"),
    )

    summary = await service.get_user_cost(user_id=7)

    assert summary.estimated_cost == Decimal("0.0001")
