"""Bar-by-bar backtester.

Semantics:

- On every bar, the strategy's :meth:`on_bar` is called **after** the bar
  closes; returned signals are executed at the **next** bar's open to avoid
  look-ahead bias.
- One position per symbol at a time. An ``ENTER_*`` signal while already in
  the opposite direction flips the position (closes the old one, opens the
  new one, both at the next bar's open).
- Stop-loss and take-profit are evaluated intra-bar using the bar's high/low.
  If both would trigger in the same bar the stop-loss wins (conservative).
- Fees and slippage are expressed in basis points of notional (``1 bp =
  0.01 %``). Both sides of every trade pay them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from archangel.backtest.metrics import PerformanceMetrics, compute_metrics
from archangel.strategy.base import Bar, Signal, SignalAction, Strategy


@dataclass(frozen=True)
class Trade:
    """A single closed round-trip."""

    side: str  # LONG / SHORT
    entry_ms: int
    exit_ms: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    fee: Decimal
    reason_in: str
    reason_out: str


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    initial_equity: Decimal
    final_equity: Decimal
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[int, Decimal]] = field(default_factory=list)  # (ms, equity)
    metrics: PerformanceMetrics | None = None


@dataclass
class _OpenPosition:
    side: str  # LONG / SHORT
    entry_ms: int
    entry_price: Decimal
    quantity: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    entry_fee: Decimal
    reason_in: str


class Backtester:
    """Run a strategy over a sequence of bars and collect trades + metrics."""

    def __init__(
        self,
        *,
        initial_equity: Decimal = Decimal("10000"),
        risk_per_trade_pct: Decimal = Decimal("1.0"),
        fee_bps: Decimal = Decimal("4"),
        slippage_bps: Decimal = Decimal("2"),
        leverage: Decimal = Decimal("1"),
    ) -> None:
        if initial_equity <= 0:
            raise ValueError("initial_equity must be > 0")
        if risk_per_trade_pct <= 0:
            raise ValueError("risk_per_trade_pct must be > 0")
        self._initial = Decimal(str(initial_equity))
        self._risk_pct = Decimal(str(risk_per_trade_pct))
        self._fee_bps = Decimal(str(fee_bps))
        self._slip_bps = Decimal(str(slippage_bps))
        self._leverage = Decimal(str(leverage))

    def run(
        self,
        strategy: Strategy,
        bars: Iterable[Bar],
        *,
        symbol: str = "",
    ) -> BacktestResult:
        bars = list(bars)
        equity = self._initial
        position: _OpenPosition | None = None
        trades: list[Trade] = []
        curve: list[tuple[int, Decimal]] = []
        # Signal returned on bar i is executed at bar i+1's open.
        pending: Signal | None = None

        for bar in bars:
            # 1. Intra-bar stop/TP check on any open position carried in from
            # the previous bar.
            if position is not None:
                exit_info = _check_intra_bar_exit(position, bar)
                if exit_info is not None:
                    exit_price, reason = exit_info
                    trade, equity = _close_position(
                        position, bar.close_ms, exit_price, reason, equity, self._fee_bps
                    )
                    trades.append(trade)
                    position = None

            # 2. Execute any pending signal from the previous bar at this bar's open.
            if pending is not None:
                action = pending.action
                exec_price = _apply_slippage(bar.open, action, self._slip_bps)
                if action == SignalAction.EXIT and position is not None:
                    trade, equity = _close_position(
                        position,
                        bar.open_ms,
                        exec_price,
                        pending.reason or "EXIT",
                        equity,
                        self._fee_bps,
                    )
                    trades.append(trade)
                    position = None
                elif action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
                    desired_side = "LONG" if action == SignalAction.ENTER_LONG else "SHORT"
                    if position is not None and position.side != desired_side:
                        # Flip: close existing first.
                        trade, equity = _close_position(
                            position,
                            bar.open_ms,
                            exec_price,
                            "flip",
                            equity,
                            self._fee_bps,
                        )
                        trades.append(trade)
                        position = None
                    if position is None:
                        qty = _size_from_risk(
                            equity=equity,
                            risk_pct=self._risk_pct,
                            entry=exec_price,
                            stop=pending.stop_loss,
                            leverage=self._leverage,
                        )
                        if qty > 0:
                            fee = exec_price * qty * self._fee_bps / Decimal("10000")
                            equity -= fee
                            position = _OpenPosition(
                                side=desired_side,
                                entry_ms=bar.open_ms,
                                entry_price=exec_price,
                                quantity=qty,
                                stop_loss=pending.stop_loss,
                                take_profit=pending.take_profit,
                                entry_fee=fee,
                                reason_in=pending.reason or desired_side,
                            )
                pending = None

            # 2b. If we just opened a position on this bar, still evaluate
            # its SL/TP against the rest of the bar's range. This keeps
            # semantics consistent with real execution: a stop placed at
            # 95 after entering at 100 is hit immediately if the bar's low
            # is 94.
            if position is not None:
                exit_info = _check_intra_bar_exit(position, bar)
                if exit_info is not None:
                    exit_price, reason = exit_info
                    trade, equity = _close_position(
                        position, bar.close_ms, exit_price, reason, equity, self._fee_bps
                    )
                    trades.append(trade)
                    position = None

            # 3. Now let the strategy see this closed bar.
            signal = strategy.on_bar(bar)
            if signal is not None and signal.action != SignalAction.HOLD:
                pending = signal

            # 4. Record equity (mark-to-market).
            curve.append((bar.close_ms, _mark_equity(equity, position, bar.close)))

        # Close any dangling position at last bar close.
        if position is not None and bars:
            last = bars[-1]
            trade, equity = _close_position(
                position,
                last.close_ms,
                last.close,
                "end-of-backtest",
                equity,
                self._fee_bps,
            )
            trades.append(trade)
            position = None
            curve[-1] = (last.close_ms, equity)

        metrics = compute_metrics(
            initial_equity=self._initial,
            final_equity=equity,
            trades=trades,
            equity_curve=curve,
        )
        return BacktestResult(
            symbol=symbol,
            strategy=strategy.name or strategy.__class__.__name__,
            initial_equity=self._initial,
            final_equity=equity,
            trades=trades,
            equity_curve=curve,
            metrics=metrics,
        )


# ---- helpers --------------------------------------------------------------


def _apply_slippage(price: Decimal, action: SignalAction, slip_bps: Decimal) -> Decimal:
    slip = price * slip_bps / Decimal("10000")
    if action == SignalAction.ENTER_LONG:
        return price + slip
    if action == SignalAction.ENTER_SHORT:
        return price - slip
    return price  # EXIT: neutral; the close_position helper applies fees only


def _size_from_risk(
    *,
    equity: Decimal,
    risk_pct: Decimal,
    entry: Decimal,
    stop: Decimal | None,
    leverage: Decimal,
) -> Decimal:
    """Quantity such that loss at stop ≈ risk_pct of equity.

    Falls back to a simple leveraged sizing if no stop is set.
    """
    if entry <= 0:
        return Decimal("0")
    if stop is None or stop <= 0 or stop == entry:
        notional = equity * leverage
        return (notional / entry).quantize(Decimal("0.00000001"))
    risk_budget = equity * risk_pct / Decimal("100")
    per_unit = abs(entry - stop)
    if per_unit == 0:
        return Decimal("0")
    qty = risk_budget / per_unit
    return qty.quantize(Decimal("0.00000001"))


def _check_intra_bar_exit(pos: _OpenPosition, bar: Bar) -> tuple[Decimal, str] | None:
    """Return (exit_price, reason) if SL or TP triggers inside this bar."""
    sl, tp = pos.stop_loss, pos.take_profit
    if pos.side == "LONG":
        if sl is not None and bar.low <= sl:
            return sl, "stop-loss"
        if tp is not None and bar.high >= tp:
            return tp, "take-profit"
    else:  # SHORT
        if sl is not None and bar.high >= sl:
            return sl, "stop-loss"
        if tp is not None and bar.low <= tp:
            return tp, "take-profit"
    return None


def _close_position(
    pos: _OpenPosition,
    exit_ms: int,
    exit_price: Decimal,
    reason: str,
    equity: Decimal,
    fee_bps: Decimal,
) -> tuple[Trade, Decimal]:
    """Close a position. Returns (trade, new_equity).

    ``trade.pnl`` is net of **both** the entry fee (already deducted from
    equity when opening) and the exit fee, so ``sum(t.pnl for t in trades)``
    equals ``final_equity - initial_equity`` for a fully-closed session.
    """
    exit_fee = exit_price * pos.quantity * fee_bps / Decimal("10000")
    gross = (exit_price - pos.entry_price) * pos.quantity
    if pos.side == "SHORT":
        gross = -gross
    total_fee = pos.entry_fee + exit_fee
    pnl = gross - total_fee
    # Equity change for the close leg: pnl + entry_fee (since entry_fee was
    # already subtracted when the position opened). That reduces to
    # gross - exit_fee.
    new_equity = equity + (gross - exit_fee)
    trade = Trade(
        side=pos.side,
        entry_ms=pos.entry_ms,
        exit_ms=exit_ms,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        quantity=pos.quantity,
        pnl=pnl,
        fee=total_fee,
        reason_in=pos.reason_in,
        reason_out=reason,
    )
    return trade, new_equity


def _mark_equity(equity: Decimal, pos: _OpenPosition | None, price: Decimal) -> Decimal:
    if pos is None:
        return equity
    unrealized = (price - pos.entry_price) * pos.quantity
    if pos.side == "SHORT":
        unrealized = -unrealized
    return equity + unrealized
