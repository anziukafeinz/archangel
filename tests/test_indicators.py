"""Unit tests for indicator primitives."""

from __future__ import annotations

from decimal import Decimal

import pytest

from archangel.strategy.indicators import atr, ema, rsi, sma


def _dec(values: list[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def test_sma_basic() -> None:
    data = _dec([1, 2, 3, 4, 5])
    result = sma(data, period=3)
    assert result[:2] == [None, None]
    assert result[2] == Decimal("2")  # (1+2+3)/3
    assert result[3] == Decimal("3")
    assert result[4] == Decimal("4")


def test_sma_rejects_zero_period() -> None:
    with pytest.raises(ValueError):
        sma([Decimal("1")], period=0)


def test_ema_seeds_from_sma() -> None:
    data = _dec([1, 2, 3, 4, 5])
    result = ema(data, period=3)
    # First two positions are None (warming up). Seed at index 2 = SMA of first 3 = 2.
    assert result[:2] == [None, None]
    assert result[2] == Decimal("2")
    # Index 3: ((4 - 2) * 2/(3+1)) + 2 = 1 + 2 = 3
    assert result[3] == Decimal("3")
    # Index 4: ((5 - 3) * 0.5) + 3 = 4
    assert result[4] == Decimal("4")


def test_ema_shorter_than_period_returns_none() -> None:
    data = _dec([1, 2])
    result = ema(data, period=5)
    assert all(v is None for v in result)


def test_rsi_range() -> None:
    # Strictly increasing prices → RSI should approach 100.
    data = _dec([float(i) for i in range(1, 30)])
    result = rsi(data, period=14)
    last = result[-1]
    assert last is not None
    assert Decimal("99") <= last <= Decimal("100")


def test_rsi_all_decreasing_is_zero() -> None:
    # Strictly decreasing prices → RSI should be 0.
    data = _dec([float(30 - i) for i in range(29)])
    result = rsi(data, period=14)
    last = result[-1]
    assert last is not None
    assert last == Decimal("0")


def test_rsi_insufficient_data_all_none() -> None:
    data = _dec([1, 2, 3])
    result = rsi(data, period=14)
    assert all(v is None for v in result)


def test_atr_requires_equal_lengths() -> None:
    with pytest.raises(ValueError):
        atr([Decimal("1"), Decimal("2")], [Decimal("1")], [Decimal("1")])


def test_atr_basic_monotonic_range() -> None:
    highs = _dec([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25])
    lows = _dec([9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24])
    closes = _dec([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25])
    result = atr(highs, lows, closes, period=14)
    # Seeded at index 13 with mean of first 14 true ranges; should be ~1.
    seed = result[13]
    assert seed is not None
    assert Decimal("0.9") <= seed <= Decimal("1.1")
