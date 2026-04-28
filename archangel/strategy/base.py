"""Strategy base class, signal types, and raw-kline parser."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any


def _d(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


class SignalAction(StrEnum):
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar with open/close timestamps in ms."""

    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def parse_kline_row(row: list) -> Bar:
    """Parse a raw Binance futures-klines row into a :class:`Bar`.

    Binance returns ``[open_ms, open, high, low, close, volume, close_ms,
    quote_vol, trades, tb_base_vol, tb_quote_vol, ignore]``.
    """
    return Bar(
        open_ms=int(row[0]),
        close_ms=int(row[6]),
        open=_d(row[1]),
        high=_d(row[2]),
        low=_d(row[3]),
        close=_d(row[4]),
        volume=_d(row[5]),
    )


@dataclass(frozen=True)
class Signal:
    """What a strategy asks the execution layer to do on the next bar.

    Prices are absolute (same units as the bar's ``close``). Stop and take-
    profit are optional but recommended; the backtester enforces them intra-
    bar using high/low, and the live paper trader translates them into
    bracket orders.
    """

    action: SignalAction
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    size_hint: Decimal | None = None  # relative size 0..1 (fraction of equity)
    reason: str = ""


class Strategy(ABC):
    """Abstract strategy with a single ``on_bar`` hook.

    Strategies are **stateful**: they accumulate history internally. Subclasses
    must implement :meth:`on_bar`; they may override :meth:`reset` to clear
    state between runs (default: no-op, the base class re-creates the instance
    for each backtest/paper session so this is usually unnecessary).
    """

    name: str = ""

    @abstractmethod
    def on_bar(self, bar: Bar) -> Signal | None:
        """Return a signal for the next bar, or ``None`` to do nothing."""

    def reset(self) -> None:  # pragma: no cover - default no-op
        """Clear any internal state. Override if needed."""
        return
