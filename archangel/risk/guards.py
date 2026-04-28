"""Pre-trade risk checks and runtime guards.

Every order placed through Archangel must pass through :class:`RiskGuard`.
The guard rejects (rather than silently fixing) anything that violates the
configured budget so that the operator always sees the failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from archangel.config import Settings
from archangel.exchange.models import AccountSnapshot, BracketOrderRequest, OrderSide


class RiskViolation(Exception):
    """Raised when a proposed order or runtime state violates the risk budget."""


@dataclass
class DailyLossTracker:
    """Tracks the equity at the start of each UTC trading day."""

    starting_equity: Decimal = Decimal("0")
    day: date = date(1970, 1, 1)

    def update_for_today(self, current_equity: Decimal) -> None:
        today = datetime.now(UTC).date()
        if today != self.day:
            self.day = today
            self.starting_equity = current_equity

    def loss_pct(self, current_equity: Decimal) -> Decimal:
        if self.starting_equity <= 0:
            return Decimal("0")
        diff = self.starting_equity - current_equity
        if diff <= 0:
            return Decimal("0")
        return (diff / self.starting_equity) * Decimal("100")


class RiskGuard:
    """Pre-trade and runtime risk checks driven by :class:`Settings`."""

    def __init__(self, settings: Settings, daily_tracker: DailyLossTracker | None = None) -> None:
        self._settings = settings
        self.daily_tracker = daily_tracker or DailyLossTracker()

    # ---- Pre-trade ----------------------------------------------------

    def validate_bracket_request(
        self, request: BracketOrderRequest, snapshot: AccountSnapshot
    ) -> None:
        """Raise :class:`RiskViolation` if the request must be rejected."""
        s = self._settings

        if s.require_stop_loss and request.stop_loss_price <= 0:
            raise RiskViolation("Stop-loss is required for every entry")

        # Stop-loss must be on the protective side of the entry price.
        ref_price = request.entry_price
        if ref_price is None or ref_price <= 0:
            # Can't fully validate without a reference price; the caller is
            # expected to pass the current mark price for market orders.
            raise RiskViolation("Reference price is required to validate the stop-loss direction")
        if request.side is OrderSide.BUY and request.stop_loss_price >= ref_price:
            raise RiskViolation(
                "For a long entry, stop-loss must be strictly below the entry price"
            )
        if request.side is OrderSide.SELL and request.stop_loss_price <= ref_price:
            raise RiskViolation(
                "For a short entry, stop-loss must be strictly above the entry price"
            )

        if request.take_profit_price is not None:
            if request.side is OrderSide.BUY and request.take_profit_price <= ref_price:
                raise RiskViolation(
                    "For a long entry, take-profit must be strictly above the entry price"
                )
            if request.side is OrderSide.SELL and request.take_profit_price >= ref_price:
                raise RiskViolation(
                    "For a short entry, take-profit must be strictly below the entry price"
                )

        # Position cap (count an existing position on the same symbol as 1 slot).
        symbols_open = {p.symbol for p in snapshot.positions}
        if request.symbol.upper() not in symbols_open and len(symbols_open) >= s.max_open_positions:
            raise RiskViolation(f"Already at max_open_positions={s.max_open_positions}")

        # Risk budget check: |entry-stop| * qty must not exceed the budget.
        equity = snapshot.total_margin_balance
        if equity <= 0:
            raise RiskViolation("Account equity is zero or negative")
        budget = equity * (Decimal(str(s.risk_per_trade_pct)) / Decimal("100"))
        risk_per_unit = abs(ref_price - request.stop_loss_price)
        notional_risk = risk_per_unit * request.quantity
        # Allow a 1% over-shoot to absorb tick rounding without rejecting trades.
        if notional_risk > budget * Decimal("1.01"):
            raise RiskViolation(
                f"Order risks {notional_risk:.4f} USDT but per-trade budget is "
                f"{budget:.4f} USDT ({s.risk_per_trade_pct}% of {equity:.4f})"
            )

        # Daily loss circuit breaker.
        self.daily_tracker.update_for_today(equity)
        loss_pct = self.daily_tracker.loss_pct(equity)
        if loss_pct >= Decimal(str(s.max_daily_loss_pct)):
            raise RiskViolation(
                f"Daily loss limit hit: down {loss_pct:.2f}% from "
                f"{self.daily_tracker.starting_equity:.2f} (limit "
                f"{s.max_daily_loss_pct}%). Trading paused for the rest of the UTC day."
            )

    # ---- Runtime ------------------------------------------------------

    def should_trade(self, snapshot: AccountSnapshot) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a generic 'is trading allowed now?' check."""
        s = self._settings
        equity = snapshot.total_margin_balance
        if equity <= 0:
            return False, "Account equity is zero or negative"
        self.daily_tracker.update_for_today(equity)
        loss_pct = self.daily_tracker.loss_pct(equity)
        if loss_pct >= Decimal(str(s.max_daily_loss_pct)):
            return False, (
                f"Daily loss limit hit: down {loss_pct:.2f}% (limit {s.max_daily_loss_pct}%)"
            )
        return True, "ok"
