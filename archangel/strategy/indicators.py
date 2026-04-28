"""Pure, dependency-free technical indicators over :class:`Decimal` series.

Every function operates on a list of prices (or OHLC triples) and returns a
list of the same length with ``None`` in positions that don't yet have enough
data. This shape makes them trivial to backtest bar-by-bar and also easy to
unit-test against textbook values.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

Series = list[Decimal | None]


def sma(values: Iterable[Decimal], period: int) -> Series:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be > 0")
    data = list(values)
    out: Series = [None] * len(data)
    running = Decimal("0")
    for i, v in enumerate(data):
        running += v
        if i >= period:
            running -= data[i - period]
        if i + 1 >= period:
            out[i] = running / Decimal(period)
    return out


def ema(values: Iterable[Decimal], period: int) -> Series:
    """Exponential moving average seeded with the SMA of the first ``period`` values."""
    if period <= 0:
        raise ValueError("period must be > 0")
    data = list(values)
    out: Series = [None] * len(data)
    if len(data) < period:
        return out
    seed = sum(data[:period], Decimal("0")) / Decimal(period)
    out[period - 1] = seed
    alpha = Decimal(2) / (Decimal(period) + Decimal(1))
    for i in range(period, len(data)):
        prev = out[i - 1]
        assert prev is not None  # seeded above, then filled in order
        out[i] = (data[i] - prev) * alpha + prev
    return out


def rolling_mean(values: Iterable[Decimal], period: int) -> Series:
    """Alias for :func:`sma`, reads nicer in some call sites."""
    return sma(values, period)


def rsi(values: Iterable[Decimal], period: int = 14) -> Series:
    """Wilder's RSI.

    Returns values in [0, 100]. Positions before ``period`` diffs (i.e.
    index < period) are ``None``.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    data = list(values)
    out: Series = [None] * len(data)
    if len(data) <= period:
        return out
    # Initial averages across the first `period` changes (indices 1..period).
    gain = Decimal("0")
    loss = Decimal("0")
    for i in range(1, period + 1):
        diff = data[i] - data[i - 1]
        if diff >= 0:
            gain += diff
        else:
            loss += -diff
    avg_gain = gain / Decimal(period)
    avg_loss = loss / Decimal(period)
    out[period] = _rsi_from_averages(avg_gain, avg_loss)
    for i in range(period + 1, len(data)):
        diff = data[i] - data[i - 1]
        up = diff if diff > 0 else Decimal("0")
        down = -diff if diff < 0 else Decimal("0")
        avg_gain = (avg_gain * (period - 1) + up) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + down) / Decimal(period)
        out[i] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal("100") if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def atr(
    highs: Iterable[Decimal],
    lows: Iterable[Decimal],
    closes: Iterable[Decimal],
    period: int = 14,
) -> Series:
    """Wilder's Average True Range.

    Takes parallel high/low/close series (same length). Returns ``None`` in
    the first ``period`` positions.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    hs, ls, cs = list(highs), list(lows), list(closes)
    if not (len(hs) == len(ls) == len(cs)):
        raise ValueError("high/low/close series must have equal length")
    n = len(cs)
    out: Series = [None] * n
    if n <= period:
        return out
    # True ranges
    trs: list[Decimal] = [Decimal("0")] * n
    trs[0] = hs[0] - ls[0]
    for i in range(1, n):
        tr = max(
            hs[i] - ls[i],
            abs(hs[i] - cs[i - 1]),
            abs(ls[i] - cs[i - 1]),
        )
        trs[i] = tr
    # Seed with simple mean of the first `period` TRs (indices 0..period-1)
    seed = sum(trs[:period], Decimal("0")) / Decimal(period)
    out[period - 1] = seed
    for i in range(period, n):
        prev = out[i - 1]
        assert prev is not None
        out[i] = (prev * (period - 1) + trs[i]) / Decimal(period)
    return out
