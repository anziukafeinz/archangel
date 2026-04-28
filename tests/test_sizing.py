"""Unit tests for position sizing."""

from decimal import Decimal

import pytest

from archangel.risk.sizing import position_size_from_risk


def test_basic_sizing_long() -> None:
    qty = position_size_from_risk(
        equity=Decimal("10000"),
        risk_pct=Decimal("1"),  # risk 100 USDT
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),  # 5 USDT distance
    )
    # 100 / 5 = 20
    assert qty == Decimal("20")


def test_basic_sizing_short() -> None:
    qty = position_size_from_risk(
        equity=Decimal("10000"),
        risk_pct=Decimal("0.5"),  # risk 50 USDT
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("110"),  # 10 USDT distance
    )
    assert qty == Decimal("5")


def test_qty_step_rounds_down() -> None:
    qty = position_size_from_risk(
        equity=Decimal("1000"),
        risk_pct=Decimal("1"),  # risk 10 USDT
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("97"),  # distance 3 USDT -> raw qty 3.333...
        qty_step=Decimal("0.1"),
    )
    assert qty == Decimal("3.3")


def test_below_min_qty_returns_zero() -> None:
    qty = position_size_from_risk(
        equity=Decimal("100"),
        risk_pct=Decimal("0.1"),  # risk 0.1 USDT
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("90"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("1"),
    )
    assert qty == Decimal("0")


def test_zero_distance_raises() -> None:
    with pytest.raises(ValueError):
        position_size_from_risk(
            equity=Decimal("1000"),
            risk_pct=Decimal("1"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("100"),
        )


def test_non_positive_inputs_raise() -> None:
    with pytest.raises(ValueError):
        position_size_from_risk(
            equity=Decimal("0"),
            risk_pct=Decimal("1"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("95"),
        )
    with pytest.raises(ValueError):
        position_size_from_risk(
            equity=Decimal("1000"),
            risk_pct=Decimal("0"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("95"),
        )


def test_risk_budget_is_actually_respected() -> None:
    """Quantity * |entry - stop| must never exceed the risk budget."""
    qty = position_size_from_risk(
        equity=Decimal("1234.56"),
        risk_pct=Decimal("0.75"),
        entry_price=Decimal("27345.10"),
        stop_loss_price=Decimal("27100.00"),
        qty_step=Decimal("0.001"),
    )
    risk_budget = Decimal("1234.56") * Decimal("0.0075")
    realized = qty * (Decimal("27345.10") - Decimal("27100.00"))
    assert realized <= risk_budget
