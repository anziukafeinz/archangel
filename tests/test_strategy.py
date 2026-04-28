"""Unit tests for the built-in strategies."""

from __future__ import annotations

import math
from decimal import Decimal

from archangel.strategy import (
    EmaCrossStrategy,
    RsiReversionStrategy,
    SignalAction,
    get_strategy,
    parse_kline_row,
)
from archangel.strategy.base import Bar


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


def test_parse_kline_row_shape() -> None:
    row = [
        1700000000000,
        "100.5",
        "101.0",
        "99.5",
        "100.0",
        "12.345",
        1700000059999,
        "1234.5",
        50,
        "6.0",
        "600",
        "0",
    ]
    bar = parse_kline_row(row)
    assert bar.open_ms == 1700000000000
    assert bar.close_ms == 1700000059999
    assert bar.open == Decimal("100.5")
    assert bar.high == Decimal("101.0")
    assert bar.low == Decimal("99.5")
    assert bar.close == Decimal("100.0")
    assert bar.volume == Decimal("12.345")


def test_registry_round_trip() -> None:
    strat = get_strategy("ema_cross")
    assert isinstance(strat, EmaCrossStrategy)
    strat2 = get_strategy("rsi_reversion")
    assert isinstance(strat2, RsiReversionStrategy)


def test_registry_unknown_raises() -> None:
    import pytest

    with pytest.raises(KeyError):
        get_strategy("nonexistent_strategy")


def test_ema_cross_fires_long_on_uptrend() -> None:
    """Feed a sine that dips then rises strongly; expect a LONG signal."""
    strat = EmaCrossStrategy(fast=3, slow=8, atr_period=5)
    signals: list[SignalAction] = []
    # 40 bars: first half declining, second half rising
    for i in range(40):
        price = 100 - i * 0.5 if i < 15 else 100 - 15 * 0.5 + (i - 15) * 2.0
        bar = _bar(i, price, price + 0.5, price - 0.5, price)
        sig = strat.on_bar(bar)
        if sig is not None:
            signals.append(sig.action)
    # Should have crossed up at least once during the rally.
    assert SignalAction.ENTER_LONG in signals


def test_ema_cross_reset_clears_state() -> None:
    strat = EmaCrossStrategy(fast=3, slow=8, atr_period=5)
    for i in range(15):
        strat.on_bar(_bar(i, 100, 101, 99, 100))
    strat.reset()
    # After reset, not enough data again.
    assert strat.on_bar(_bar(0, 100, 101, 99, 100)) is None


def test_ema_cross_rejects_fast_ge_slow() -> None:
    import pytest

    with pytest.raises(ValueError):
        EmaCrossStrategy(fast=10, slow=10)


def test_rsi_reversion_enters_long_when_oversold() -> None:
    """Sharp decline pushes RSI low enough to trigger ENTER_LONG."""
    strat = RsiReversionStrategy(
        period=7,
        oversold=Decimal("30"),
        overbought=Decimal("70"),
        atr_period=7,
    )
    signals: list[SignalAction] = []
    # 30 bars of steady decline
    for i in range(30):
        price = 100 - i * 1.5
        bar = _bar(i, price, price + 0.3, price - 0.3, price)
        sig = strat.on_bar(bar)
        if sig is not None:
            signals.append(sig.action)
    assert SignalAction.ENTER_LONG in signals


def test_rsi_reversion_rejects_bad_bands() -> None:
    import pytest

    with pytest.raises(ValueError):
        RsiReversionStrategy(oversold=Decimal("70"), overbought=Decimal("30"))


def test_signal_has_stop_when_strategy_provides_one() -> None:
    strat = EmaCrossStrategy(fast=3, slow=8, atr_period=5, atr_stop_mult=Decimal("2"))
    # Force a clean crossover.
    prices = [100 - i * 0.5 for i in range(15)] + [
        100 - 15 * 0.5 + (j + 1) * 3.0 for j in range(15)
    ]
    found = False
    for i, p in enumerate(prices):
        sig = strat.on_bar(_bar(i, p, p + 0.5, p - 0.5, p))
        if sig and sig.action == SignalAction.ENTER_LONG:
            assert sig.stop_loss is not None
            assert sig.stop_loss < Decimal(str(p))
            found = True
            break
    assert found, "expected at least one long entry in the rally"


def _smoke_bars(n: int) -> list[Bar]:
    """Gently noisy price series for smoke-testing strategies."""
    bars: list[Bar] = []
    for i in range(n):
        price = 100 + 5 * math.sin(i / 3.0)
        bars.append(_bar(i, price, price + 0.2, price - 0.2, price))
    return bars


def test_strategies_never_crash_on_random_input() -> None:
    for name in ("ema_cross", "rsi_reversion"):
        strat = get_strategy(name)
        for bar in _smoke_bars(100):
            strat.on_bar(bar)
