"""Unit tests for user-data event parsing and formatting."""

from decimal import Decimal

import pytest

from archangel.streams.user_data import (
    AccountUpdate,
    EventKind,
    MarginCallEvent,
    OrderUpdate,
    parse_event,
)
from archangel.streams.watcher import (
    format_account_update,
    format_event,
    format_margin_call,
    format_order_update,
)

# ---- Fixtures (anonymized samples cribbed from Binance docs) -------


def order_fill_payload() -> dict:
    return {
        "e": "ORDER_TRADE_UPDATE",
        "T": 1568879465650,
        "E": 1568879465651,
        "o": {
            "s": "BTCUSDT",
            "c": "arc-en-abc",
            "S": "BUY",
            "o": "MARKET",
            "f": "GTC",
            "q": "0.500",
            "p": "0",
            "ap": "31000.00",
            "sp": "0",
            "x": "TRADE",
            "X": "FILLED",
            "i": 8886774,
            "l": "0.500",
            "z": "0.500",
            "L": "31000.00",
            "n": "0.31",
            "N": "USDT",
            "rp": "12.50",
            "R": False,
            "cp": False,
            "ot": "MARKET",
        },
    }


def order_canceled_payload() -> dict:
    return {
        "e": "ORDER_TRADE_UPDATE",
        "T": 1568879465650,
        "E": 1568879465651,
        "o": {
            "s": "BTCUSDT",
            "c": "arc-sl-xyz",
            "S": "SELL",
            "o": "STOP_MARKET",
            "f": "GTC",
            "q": "0.500",
            "p": "0",
            "ap": "0",
            "sp": "29000.00",
            "x": "CANCELED",
            "X": "CANCELED",
            "i": 8886775,
            "l": "0",
            "z": "0",
            "L": "0",
            "n": "0",
            "N": "",
            "rp": "0",
            "R": False,
            "cp": True,
            "ot": "STOP_MARKET",
        },
    }


def order_new_payload() -> dict:
    p = order_canceled_payload()
    p["o"]["x"] = "NEW"
    p["o"]["X"] = "NEW"
    return p


def account_update_payload() -> dict:
    return {
        "e": "ACCOUNT_UPDATE",
        "T": 1568879465650,
        "E": 1568879465651,
        "a": {
            "m": "ORDER",
            "B": [
                {"a": "USDT", "wb": "1000.00", "cw": "1000.00", "bc": "12.50"},
            ],
            "P": [
                {
                    "s": "BTCUSDT",
                    "pa": "0.500",
                    "ep": "31000.00",
                    "cr": "0",
                    "up": "5.25",
                    "mt": "isolated",
                    "iw": "100.00",
                    "ps": "BOTH",
                }
            ],
        },
    }


def margin_call_payload() -> dict:
    return {
        "e": "MARGIN_CALL",
        "E": 1587727187525,
        "cw": "3.16812045",
        "p": [
            {
                "s": "ETHUSDT",
                "ps": "LONG",
                "pa": "1.327",
                "mt": "CROSSED",
                "iw": "0",
                "mp": "1500.0",
                "up": "-100.0",
                "mm": "5.0",
            }
        ],
    }


# ---- Parser tests --------------------------------------------------


def test_parse_order_fill() -> None:
    event = parse_event(order_fill_payload())
    assert isinstance(event, OrderUpdate)
    assert event.kind is EventKind.ORDER_UPDATE
    assert event.symbol == "BTCUSDT"
    assert event.side == "BUY"
    assert event.order_status == "FILLED"
    assert event.execution_type == "TRADE"
    assert event.last_filled_quantity == Decimal("0.500")
    assert event.last_filled_price == Decimal("31000.00")
    assert event.realized_pnl == Decimal("12.50")
    assert event.commission == Decimal("0.31")
    assert event.commission_asset == "USDT"
    assert event.is_fill is True


def test_parse_order_canceled() -> None:
    event = parse_event(order_canceled_payload())
    assert isinstance(event, OrderUpdate)
    assert event.order_status == "CANCELED"
    assert event.is_close_position is True
    assert event.is_fill is False
    assert event.stop_price == Decimal("29000.00")


def test_parse_account_update() -> None:
    event = parse_event(account_update_payload())
    assert isinstance(event, AccountUpdate)
    assert event.kind is EventKind.ACCOUNT_UPDATE
    assert event.reason == "ORDER"
    assert len(event.balances) == 1
    assert event.balances[0].asset == "USDT"
    assert event.balances[0].balance_change == Decimal("12.50")
    assert len(event.positions) == 1
    pos = event.positions[0]
    assert pos.symbol == "BTCUSDT"
    assert pos.quantity == Decimal("0.500")
    assert pos.entry_price == Decimal("31000.00")
    assert pos.unrealized_pnl == Decimal("5.25")
    assert pos.margin_type == "isolated"


def test_parse_margin_call() -> None:
    event = parse_event(margin_call_payload())
    assert isinstance(event, MarginCallEvent)
    assert event.kind is EventKind.MARGIN_CALL
    assert event.cross_wallet_balance == Decimal("3.16812045")
    assert len(event.positions) == 1
    p = event.positions[0]
    assert p.symbol == "ETHUSDT"
    assert p.position_side == "LONG"
    assert p.unrealized_pnl == Decimal("-100.0")


def test_parse_unknown_event_returns_other() -> None:
    event = parse_event({"e": "SOMETHING_NEW", "E": 123})
    assert event.kind is EventKind.OTHER
    assert event.event_time_ms == 123


def test_parse_account_config_update() -> None:
    event = parse_event({"e": "ACCOUNT_CONFIG_UPDATE", "E": 1, "ac": {}})
    assert event.kind is EventKind.ACCOUNT_CONFIG_UPDATE


def test_parse_non_dict_raises() -> None:
    with pytest.raises(TypeError):
        parse_event("not a dict")  # type: ignore[arg-type]


# ---- Formatter tests -----------------------------------------------


def test_format_order_fill_emits_message() -> None:
    event = parse_event(order_fill_payload())
    assert isinstance(event, OrderUpdate)
    text = format_order_update(event)
    assert text is not None
    assert "FILLED" in text
    assert "BTCUSDT" in text
    assert "0.500" in text
    assert "+12.5000" in text or "12.50" in text


def test_format_order_new_is_suppressed() -> None:
    event = parse_event(order_new_payload())
    assert isinstance(event, OrderUpdate)
    assert format_order_update(event) is None


def test_format_account_update_with_position() -> None:
    event = parse_event(account_update_payload())
    assert isinstance(event, AccountUpdate)
    text = format_account_update(event)
    assert text is not None
    assert "BTCUSDT" in text
    assert "Account update" in text


def test_format_margin_call_always_emits() -> None:
    event = parse_event(margin_call_payload())
    assert isinstance(event, MarginCallEvent)
    text = format_margin_call(event)
    assert "MARGIN CALL" in text
    assert "ETHUSDT" in text
    assert "1500.0" in text


def test_format_event_dispatches_by_type() -> None:
    assert format_event(parse_event(order_fill_payload())) is not None
    assert format_event(parse_event(margin_call_payload())) is not None
    assert format_event(parse_event(account_update_payload())) is not None
    assert format_event(parse_event(order_new_payload())) is None
