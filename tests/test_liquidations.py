"""Unit tests for the liquidation-feed parser and formatter."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from archangel.streams.liquidations import (
    LiquidationStream,
    format_liquidation,
    parse_liquidation,
)

# Canonical sample from Binance docs:
# https://binance-docs.github.io/apidocs/futures/en/#liquidation-order-streams
SAMPLE_PAYLOAD = {
    "e": "forceOrder",
    "E": 1568014460894,
    "o": {
        "s": "BTCUSDT",
        "S": "SELL",
        "o": "LIMIT",
        "f": "IOC",
        "q": "0.014",
        "p": "9910",
        "ap": "9910",
        "X": "FILLED",
        "l": "0.014",
        "z": "0.014",
        "T": 1568014460893,
    },
}


def test_parse_liquidation_basic() -> None:
    ev = parse_liquidation(SAMPLE_PAYLOAD)
    assert ev.symbol == "BTCUSDT"
    assert ev.side == "SELL"
    assert ev.order_status == "FILLED"
    assert ev.original_quantity == Decimal("0.014")
    assert ev.average_price == Decimal("9910")
    assert ev.filled_accumulated_quantity == Decimal("0.014")
    assert ev.trade_time_ms == 1568014460893
    assert ev.event_time_ms == 1568014460894


def test_parse_liquidation_accepts_json_string() -> None:
    ev = parse_liquidation(json.dumps(SAMPLE_PAYLOAD))
    assert ev.symbol == "BTCUSDT"


def test_parse_liquidation_accepts_bytes() -> None:
    ev = parse_liquidation(json.dumps(SAMPLE_PAYLOAD).encode("utf-8"))
    assert ev.symbol == "BTCUSDT"


def test_parse_liquidation_unwraps_multiplex_envelope() -> None:
    wrapped = {"stream": "btcusdt@forceOrder", "data": SAMPLE_PAYLOAD}
    ev = parse_liquidation(wrapped)
    assert ev.symbol == "BTCUSDT"


def test_parse_liquidation_rejects_other_events() -> None:
    with pytest.raises(ValueError, match="Not a forceOrder"):
        parse_liquidation({"e": "kline", "E": 1, "o": {}})


def test_parse_liquidation_rejects_non_dict() -> None:
    with pytest.raises(TypeError):
        parse_liquidation(42)  # type: ignore[arg-type]


def test_parse_liquidation_missing_o_object() -> None:
    with pytest.raises(ValueError, match="missing 'o'"):
        parse_liquidation({"e": "forceOrder", "E": 1})


def test_notional_usd_uses_avg_price() -> None:
    ev = parse_liquidation(SAMPLE_PAYLOAD)
    # 0.014 BTC * 9910 USD = 138.74
    assert ev.notional_usd == Decimal("138.74")


def test_notional_usd_falls_back_to_order_price() -> None:
    payload = {
        "e": "forceOrder",
        "E": 1,
        "o": {
            "s": "ETHUSDT",
            "S": "BUY",
            "o": "LIMIT",
            "f": "IOC",
            "q": "2",
            "p": "3000",
            "ap": "0",  # not filled yet
            "X": "NEW",
            "l": "0",
            "z": "0",
            "T": 1,
        },
    }
    ev = parse_liquidation(payload)
    assert ev.notional_usd == Decimal("6000.00")


def test_flattened_side_long_for_sell() -> None:
    """A SELL liquidation flattens a long position."""
    ev = parse_liquidation(SAMPLE_PAYLOAD)
    assert ev.flattened_side == "LONG"


def test_flattened_side_short_for_buy() -> None:
    payload = dict(SAMPLE_PAYLOAD)
    payload["o"] = dict(SAMPLE_PAYLOAD["o"], S="BUY")
    ev = parse_liquidation(payload)
    assert ev.flattened_side == "SHORT"


def test_format_liquidation_contains_key_fields() -> None:
    ev = parse_liquidation(SAMPLE_PAYLOAD)
    line = format_liquidation(ev)
    assert "BTCUSDT" in line
    assert "LONG liquidated" in line
    assert "9910" in line
    assert "139" in line  # notional rounded via :,.0f (138.74 → 139)


def test_stream_name_defaults_to_all() -> None:
    class _Stub:
        client = object()

    stream = LiquidationStream(_Stub(), symbols=None)  # type: ignore[arg-type]
    assert stream._stream_names() == ["!forceOrder@arr"]


def test_stream_name_lowercases_symbols() -> None:
    class _Stub:
        client = object()

    stream = LiquidationStream(_Stub(), symbols=["BTCUSDT", "ethusdt"])  # type: ignore[arg-type]
    assert stream._stream_names() == ["btcusdt@forceOrder", "ethusdt@forceOrder"]
