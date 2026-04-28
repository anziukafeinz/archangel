"""Unit tests for the pre-trade risk guard."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from archangel.config import Settings
from archangel.exchange.models import (
    AccountSnapshot,
    BracketOrderRequest,
    OrderSide,
    Position,
)
from archangel.risk.guards import DailyLossTracker, RiskGuard, RiskViolation


def _snapshot(equity: Decimal, positions: list[Position] | None = None) -> AccountSnapshot:
    return AccountSnapshot(
        total_wallet_balance=equity,
        total_margin_balance=equity,
        available_balance=equity,
        total_unrealized_pnl=Decimal("0"),
        positions=positions or [],
    )


def _settings(**overrides) -> Settings:
    base = dict(
        binance_api_key="x",
        binance_api_secret="x",
        binance_testnet=True,
        risk_per_trade_pct=1.0,
        max_open_positions=5,
        max_daily_loss_pct=5.0,
        default_leverage=5,
        require_stop_loss=True,
    )
    base.update(overrides)
    return Settings(**base)


def test_long_stop_above_entry_rejected() -> None:
    guard = RiskGuard(_settings())
    req = BracketOrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("110"),
    )
    with pytest.raises(RiskViolation):
        guard.validate_bracket_request(req, _snapshot(Decimal("1000")))


def test_short_stop_below_entry_rejected() -> None:
    guard = RiskGuard(_settings())
    req = BracketOrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        quantity=Decimal("0.01"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("90"),
    )
    with pytest.raises(RiskViolation):
        guard.validate_bracket_request(req, _snapshot(Decimal("1000")))


def test_missing_stop_loss_rejected() -> None:
    guard = RiskGuard(_settings(require_stop_loss=True))
    req = BracketOrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("0"),
    )
    with pytest.raises(RiskViolation):
        guard.validate_bracket_request(req, _snapshot(Decimal("1000")))


def test_risk_budget_exceeded_rejected() -> None:
    # Equity 1000, 1% budget = 10 USDT.
    # Distance 5 USDT * qty 5 = 25 USDT -> exceeds budget.
    guard = RiskGuard(_settings(risk_per_trade_pct=1.0))
    req = BracketOrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("5"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
    )
    with pytest.raises(RiskViolation):
        guard.validate_bracket_request(req, _snapshot(Decimal("1000")))


def test_within_budget_passes() -> None:
    # Equity 1000, 1% budget = 10 USDT. Distance 5 * qty 2 = 10. OK.
    guard = RiskGuard(_settings(risk_per_trade_pct=1.0))
    req = BracketOrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
    )
    guard.validate_bracket_request(req, _snapshot(Decimal("1000")))  # no raise


def test_max_open_positions_enforced() -> None:
    guard = RiskGuard(_settings(max_open_positions=2))
    pos = [
        Position(
            symbol="ETHUSDT",
            side="LONG",
            quantity=Decimal("1"),
            entry_price=Decimal("2000"),
            mark_price=Decimal("2000"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
            liquidation_price=None,
        ),
        Position(
            symbol="SOLUSDT",
            side="LONG",
            quantity=Decimal("10"),
            entry_price=Decimal("100"),
            mark_price=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
            liquidation_price=None,
        ),
    ]
    req = BracketOrderRequest(
        symbol="BTCUSDT",  # third symbol -> violates cap
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        entry_price=Decimal("30000"),
        stop_loss_price=Decimal("29800"),
    )
    with pytest.raises(RiskViolation):
        guard.validate_bracket_request(req, _snapshot(Decimal("10000"), pos))


def test_adding_to_existing_symbol_does_not_increment_count() -> None:
    guard = RiskGuard(_settings(max_open_positions=1, risk_per_trade_pct=10))
    pos = [
        Position(
            symbol="BTCUSDT",
            side="LONG",
            quantity=Decimal("0.01"),
            entry_price=Decimal("30000"),
            mark_price=Decimal("30000"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
            liquidation_price=None,
        )
    ]
    req = BracketOrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.001"),
        entry_price=Decimal("30000"),
        stop_loss_price=Decimal("29800"),
    )
    guard.validate_bracket_request(req, _snapshot(Decimal("10000"), pos))


def test_daily_loss_tracker() -> None:
    tracker = DailyLossTracker()
    today = datetime.now(UTC).date()
    tracker.update_for_today(Decimal("1000"))
    assert tracker.day == today
    assert tracker.starting_equity == Decimal("1000")

    # 5% drawdown
    pct = tracker.loss_pct(Decimal("950"))
    assert pct == Decimal("5")

    # Equity above start: no loss
    assert tracker.loss_pct(Decimal("1100")) == Decimal("0")


def test_daily_loss_limit_blocks_trade() -> None:
    settings = _settings(max_daily_loss_pct=2.0)
    tracker = DailyLossTracker(starting_equity=Decimal("1000"), day=datetime.now(UTC).date())
    guard = RiskGuard(settings, daily_tracker=tracker)
    req = BracketOrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.001"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("99"),
    )
    # current equity 970 = -3% from 1000, exceeds 2% limit
    with pytest.raises(RiskViolation, match="Daily loss"):
        guard.validate_bracket_request(req, _snapshot(Decimal("970")))
