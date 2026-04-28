"""Unit tests for funding-rate parsing and signal classification."""

from __future__ import annotations

from decimal import Decimal

from archangel.funding import (
    FundingQuote,
    SignalKind,
    annualized_yield_pct,
    classify_signals,
    parse_premium_index,
)


def test_annualized_yield_pct_basic() -> None:
    # 0.01% per 8h paid 3x/day * 365 days * 100 = 10.95%
    rate = Decimal("0.0001")
    assert annualized_yield_pct(rate) == Decimal("10.9500")


def test_annualized_yield_pct_zero() -> None:
    assert annualized_yield_pct(Decimal("0")) == Decimal("0.0000")


def test_annualized_yield_pct_negative() -> None:
    rate = Decimal("-0.0002")
    # -0.02% * 3 * 365 = -21.9%
    assert annualized_yield_pct(rate) == Decimal("-21.9000")


def test_parse_premium_index_filters_non_usdt() -> None:
    payloads = [
        {
            "symbol": "BTCUSDT",
            "markPrice": "65000",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1700000000000,
        },
        {
            "symbol": "BTCUSD_PERP",  # coin-margined; should be skipped
            "markPrice": "65000",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1700000000000,
        },
        {
            "symbol": "ETHUSDT",
            "markPrice": "3500",
            "lastFundingRate": "",  # empty rate; should be skipped
            "nextFundingTime": 1700000000000,
        },
    ]
    quotes = parse_premium_index(payloads)
    assert len(quotes) == 1
    assert quotes[0].symbol == "BTCUSDT"
    assert quotes[0].mark_price == Decimal("65000")
    assert quotes[0].last_funding_rate == Decimal("0.0001")
    assert quotes[0].next_funding_time_ms == 1700000000000


def _quote(symbol: str, rate: str) -> FundingQuote:
    return FundingQuote(
        symbol=symbol,
        mark_price=Decimal("100"),
        last_funding_rate=Decimal(rate),
        next_funding_time_ms=1700000000000,
    )


def test_classify_signals_extreme_positive() -> None:
    quotes = [_quote("BTCUSDT", "0.0008")]  # 0.08%/8h ≈ 87% APR
    signals = classify_signals(quotes, extreme_threshold=Decimal("0.0005"))
    assert len(signals) == 1
    assert signals[0].kind is SignalKind.EXTREME_POSITIVE
    assert signals[0].symbol == "BTCUSDT"


def test_classify_signals_extreme_negative() -> None:
    quotes = [_quote("ETHUSDT", "-0.0009")]
    signals = classify_signals(quotes, extreme_threshold=Decimal("0.0005"))
    assert len(signals) == 1
    assert signals[0].kind is SignalKind.EXTREME_NEGATIVE


def test_classify_signals_below_threshold_omitted() -> None:
    quotes = [
        _quote("BTCUSDT", "0.0001"),
        _quote("ETHUSDT", "0.0002"),
    ]
    assert classify_signals(quotes, extreme_threshold=Decimal("0.0005")) == []


def test_classify_signals_sign_flip() -> None:
    quotes = [_quote("BTCUSDT", "0.0001")]
    signals = classify_signals(
        quotes,
        extreme_threshold=Decimal("0.001"),  # high enough that EXTREME_* won't fire
        history_by_symbol={"BTCUSDT": [Decimal("-0.0001")]},
    )
    assert len(signals) == 1
    assert signals[0].kind is SignalKind.SIGN_FLIP
    assert "Flipped from -0.0001 to 0.0001" in signals[0].note


def test_classify_signals_no_flip_when_same_sign() -> None:
    quotes = [_quote("BTCUSDT", "0.0001")]
    signals = classify_signals(
        quotes,
        extreme_threshold=Decimal("0.001"),
        history_by_symbol={"BTCUSDT": [Decimal("0.00005")]},
    )
    assert signals == []


def test_classify_signals_sorted_by_apr() -> None:
    quotes = [
        _quote("BTCUSDT", "0.0006"),  # ~65% APR
        _quote("ETHUSDT", "-0.001"),  # -109% APR
        _quote("SOLUSDT", "0.0008"),  # 87% APR
    ]
    signals = classify_signals(quotes, extreme_threshold=Decimal("0.0005"))
    assert [s.symbol for s in signals] == ["ETHUSDT", "SOLUSDT", "BTCUSDT"]


def test_classify_signals_extreme_and_flip_both_emit() -> None:
    quotes = [_quote("BTCUSDT", "0.001")]
    signals = classify_signals(
        quotes,
        extreme_threshold=Decimal("0.0005"),
        history_by_symbol={"BTCUSDT": [Decimal("-0.0001")]},
    )
    kinds = {s.kind for s in signals}
    assert kinds == {SignalKind.EXTREME_POSITIVE, SignalKind.SIGN_FLIP}
