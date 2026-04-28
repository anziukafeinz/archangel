"""Unit tests for the trailing-stop planning helper."""

from __future__ import annotations

from decimal import Decimal

import pytest

from archangel.exchange import TrailingStopPlan, plan_trailing_stop


def test_long_position_flips_to_sell() -> None:
    plan = plan_trailing_stop(
        symbol="btcusdt",
        position_quantity=Decimal("0.5"),
        callback_rate=Decimal("1.0"),
    )
    assert isinstance(plan, TrailingStopPlan)
    assert plan.symbol == "BTCUSDT"
    assert plan.exit_side == "SELL"
    assert plan.quantity == Decimal("0.5")
    assert plan.callback_rate == Decimal("1.0")
    assert plan.activation_price is None


def test_short_position_flips_to_buy() -> None:
    plan = plan_trailing_stop(
        symbol="ETHUSDT",
        position_quantity=Decimal("-2.0"),
        callback_rate=Decimal("0.5"),
    )
    assert plan.exit_side == "BUY"
    assert plan.quantity == Decimal("2.0")  # abs of negative


def test_quantity_override_caps_position_size() -> None:
    plan = plan_trailing_stop(
        symbol="BTCUSDT",
        position_quantity=Decimal("1.0"),
        callback_rate=Decimal("1.0"),
        quantity_override=Decimal("0.4"),
    )
    assert plan.quantity == Decimal("0.4")


def test_activation_price_passes_through() -> None:
    plan = plan_trailing_stop(
        symbol="BTCUSDT",
        position_quantity=Decimal("0.5"),
        callback_rate=Decimal("1.0"),
        activation_price=Decimal("70000"),
    )
    assert plan.activation_price == Decimal("70000")


@pytest.mark.parametrize("rate", [Decimal("0.05"), Decimal("0"), Decimal("5.5"), Decimal("100")])
def test_invalid_callback_rate_rejected(rate: Decimal) -> None:
    with pytest.raises(ValueError, match="callback_rate"):
        plan_trailing_stop(
            symbol="BTCUSDT",
            position_quantity=Decimal("0.5"),
            callback_rate=rate,
        )


def test_flat_position_rejected() -> None:
    with pytest.raises(ValueError, match="flat"):
        plan_trailing_stop(
            symbol="BTCUSDT",
            position_quantity=Decimal("0"),
            callback_rate=Decimal("1.0"),
        )


@pytest.mark.parametrize("override", [Decimal("0"), Decimal("-0.1")])
def test_non_positive_override_rejected(override: Decimal) -> None:
    with pytest.raises(ValueError, match="quantity_override"):
        plan_trailing_stop(
            symbol="BTCUSDT",
            position_quantity=Decimal("0.5"),
            callback_rate=Decimal("1.0"),
            quantity_override=override,
        )


@pytest.mark.parametrize("rate", [Decimal("0.1"), Decimal("1.0"), Decimal("5")])
def test_callback_rate_boundaries_accepted(rate: Decimal) -> None:
    plan = plan_trailing_stop(
        symbol="BTCUSDT",
        position_quantity=Decimal("0.5"),
        callback_rate=rate,
    )
    assert plan.callback_rate == rate
