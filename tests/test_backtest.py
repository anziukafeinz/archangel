"""End-to-end tests for the backtest engine against deterministic synthetic bars."""

from __future__ import annotations

import math
from decimal import Decimal

from archangel.backtest import Backtester
from archangel.backtest.engine import _size_from_risk
from archangel.strategy import EmaCrossStrategy, RsiReversionStrategy
from archangel.strategy.base import Bar, Signal, SignalAction, Strategy


def _bar(i: int, o: float, h: float, lo: float, c: float) -> Bar:
    return Bar(
        open_ms=i * 60_000,
        close_ms=(i + 1) * 60_000 - 1,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(lo)),
        close=Decimal(str(c)),
        volume=Decimal("1"),
    )


class AlwaysLongOnce(Strategy):
    """Fires a single ENTER_LONG signal on the first bar, then HOLDs."""

    name = "always_long_once"

    def __init__(self, stop: Decimal, tp: Decimal | None = None) -> None:
        self._stop = stop
        self._tp = tp
        self._fired = False

    def on_bar(self, bar: Bar) -> Signal | None:
        if self._fired:
            return None
        self._fired = True
        return Signal(
            action=SignalAction.ENTER_LONG,
            stop_loss=self._stop,
            take_profit=self._tp,
            reason="test-entry",
        )


def test_size_from_risk_basic() -> None:
    qty = _size_from_risk(
        equity=Decimal("10000"),
        risk_pct=Decimal("1"),
        entry=Decimal("100"),
        stop=Decimal("95"),
        leverage=Decimal("1"),
    )
    # Budget = 100 USDT; per-unit risk = 5; qty = 20.
    assert qty == Decimal("20")


def test_size_from_risk_no_stop_uses_leverage() -> None:
    qty = _size_from_risk(
        equity=Decimal("1000"),
        risk_pct=Decimal("1"),
        entry=Decimal("50"),
        stop=None,
        leverage=Decimal("2"),
    )
    assert qty == Decimal("40")  # 1000*2/50


def test_backtest_takes_profit_exits_intra_bar() -> None:
    strat = AlwaysLongOnce(stop=Decimal("95"), tp=Decimal("105"))
    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 106, 99.8, 101),  # TP at 105 hit inside this bar
        _bar(2, 101, 102, 100, 101),
    ]
    bt = Backtester(
        initial_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    result = bt.run(strat, bars, symbol="TEST")
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_price == Decimal("105")
    assert trade.side == "LONG"
    assert trade.pnl > 0
    assert trade.reason_out == "take-profit"


def test_backtest_stop_loss_exits_intra_bar() -> None:
    strat = AlwaysLongOnce(stop=Decimal("95"), tp=Decimal("110"))
    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 101, 94, 99),  # low=94 breaks stop=95
        _bar(2, 99, 100, 98, 99),
    ]
    bt = Backtester(
        initial_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    result = bt.run(strat, bars, symbol="TEST")
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_price == Decimal("95")
    assert trade.pnl < 0
    assert trade.reason_out == "stop-loss"


def test_backtest_closes_dangling_position_at_end() -> None:
    strat = AlwaysLongOnce(stop=Decimal("50"), tp=Decimal("200"))
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 102, 100, 101),
        _bar(2, 101, 103, 101, 102),
    ]
    bt = Backtester(
        initial_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    result = bt.run(strat, bars, symbol="TEST")
    # Neither SL nor TP hit, but engine closes at final close.
    assert len(result.trades) == 1
    assert result.trades[0].reason_out == "end-of-backtest"
    assert result.trades[0].exit_price == Decimal("102")


def test_backtest_metrics_on_mean_reverting_data() -> None:
    bars: list[Bar] = []
    for i in range(200):
        price = 100 + 10 * math.sin(i / 5.0)
        bars.append(_bar(i, price, price + 0.4, price - 0.4, price))
    strat = RsiReversionStrategy(period=7, atr_period=7)
    bt = Backtester(
        initial_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
    )
    result = bt.run(strat, bars, symbol="SYN")
    # Metrics exist and are coherent.
    assert result.metrics is not None
    assert result.metrics.trade_count >= 1
    assert result.metrics.max_drawdown_pct >= 0
    # Equity should reflect the trade list.
    sum_pnl = sum((t.pnl for t in result.trades), Decimal("0"))
    assert abs(result.final_equity - (Decimal("10000") + sum_pnl)) < Decimal("1")


def test_backtest_ema_cross_on_reversal_data() -> None:
    """Classic EMA crossover should fire at a decisive trend reversal."""
    bars: list[Bar] = []
    # Phase 1: gentle decline (30 bars). Phase 2: strong rally (30 bars).
    for i in range(30):
        price = 100 - i * 0.5
        bars.append(_bar(i, price, price + 0.3, price - 0.3, price))
    for j in range(30):
        price = 85 + (j + 1) * 2.0
        bars.append(_bar(30 + j, price, price + 0.3, price - 0.3, price))
    strat = EmaCrossStrategy(fast=5, slow=15, atr_period=10)
    bt = Backtester(
        initial_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
    )
    result = bt.run(strat, bars, symbol="REV")
    assert result.metrics.trade_count >= 1


def test_backtest_equity_curve_has_one_entry_per_bar() -> None:
    strat = AlwaysLongOnce(stop=Decimal("95"))
    bars = [_bar(i, 100, 100.5, 99.5, 100) for i in range(5)]
    bt = Backtester(initial_equity=Decimal("10000"))
    result = bt.run(strat, bars, symbol="TEST")
    assert len(result.equity_curve) == len(bars)


def test_backtest_empty_bars_no_crash() -> None:
    strat = AlwaysLongOnce(stop=Decimal("95"))
    bt = Backtester()
    result = bt.run(strat, [], symbol="TEST")
    assert result.metrics.trade_count == 0
    assert result.final_equity == Decimal("10000")
