"""Binance USDT-M Futures **user data stream** consumer.

Connects to the authenticated WebSocket via :class:`BinanceSocketManager`
(which manages the listenKey lifecycle and reconnects automatically) and
parses the four event types Archangel cares about into typed dataclasses:

- ``ORDER_TRADE_UPDATE`` — every order state change, including fills, cancels
  and stop-loss triggers. Maps to :class:`OrderUpdate`.
- ``ACCOUNT_UPDATE`` — wallet balance and position changes after a fill,
  funding payment, or transfer. Maps to :class:`AccountUpdate`.
- ``MARGIN_CALL`` — emitted when the account is at risk of liquidation. Maps
  to :class:`MarginCallEvent`.
- ``ACCOUNT_CONFIG_UPDATE`` — leverage / multi-asset mode changes. Returned
  as a generic :class:`UserDataEvent`.

The parsing is a pure function (``parse_event``) so it is fully unit-testable
without any network access.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from binance import BinanceSocketManager

from archangel.exchange.binance import BinanceFuturesClient

logger = logging.getLogger(__name__)


def _d(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


class EventKind(StrEnum):
    ORDER_UPDATE = "ORDER_TRADE_UPDATE"
    ACCOUNT_UPDATE = "ACCOUNT_UPDATE"
    MARGIN_CALL = "MARGIN_CALL"
    ACCOUNT_CONFIG_UPDATE = "ACCOUNT_CONFIG_UPDATE"
    OTHER = "OTHER"


@dataclass(frozen=True)
class UserDataEvent:
    """Base envelope for all parsed user-data events."""

    kind: EventKind
    event_time_ms: int
    raw: dict


@dataclass(frozen=True)
class OrderUpdate(UserDataEvent):
    """A single ``ORDER_TRADE_UPDATE`` event.

    Field names mirror Binance's WebSocket payload (``o.x``, ``o.X`` etc.) but
    are unpacked into readable attributes. See
    https://binance-docs.github.io/apidocs/futures/en/#event-order-update
    """

    symbol: str = ""
    client_order_id: str = ""
    order_id: int = 0
    side: str = ""
    order_type: str = ""
    time_in_force: str = ""
    original_quantity: Decimal = Decimal("0")
    original_price: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    stop_price: Decimal = Decimal("0")
    execution_type: str = ""  # NEW / TRADE / CANCELED / EXPIRED / ...
    order_status: str = ""  # NEW / PARTIALLY_FILLED / FILLED / CANCELED / ...
    filled_quantity: Decimal = Decimal("0")
    last_filled_quantity: Decimal = Decimal("0")
    last_filled_price: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    commission_asset: str = ""
    realized_pnl: Decimal = Decimal("0")
    is_reduce_only: bool = False
    is_close_position: bool = False
    is_liquidation: bool = False

    @property
    def is_fill(self) -> bool:
        """True for any execution that actually moved the position."""
        return self.execution_type == "TRADE" and self.last_filled_quantity > 0


@dataclass(frozen=True)
class _AccountUpdateBalance:
    asset: str
    wallet_balance: Decimal
    cross_wallet_balance: Decimal
    balance_change: Decimal


@dataclass(frozen=True)
class _AccountUpdatePosition:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    accumulated_realized: Decimal
    unrealized_pnl: Decimal
    margin_type: str  # cross / isolated
    isolated_wallet: Decimal
    position_side: str  # LONG / SHORT / BOTH


@dataclass(frozen=True)
class AccountUpdate(UserDataEvent):
    """A parsed ``ACCOUNT_UPDATE`` event."""

    reason: str = ""
    balances: list[_AccountUpdateBalance] = field(default_factory=list)
    positions: list[_AccountUpdatePosition] = field(default_factory=list)


@dataclass(frozen=True)
class _MarginCallPosition:
    symbol: str
    position_side: str
    quantity: Decimal
    margin_type: str
    isolated_wallet: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    maintenance_margin_required: Decimal


@dataclass(frozen=True)
class MarginCallEvent(UserDataEvent):
    """A parsed ``MARGIN_CALL`` event."""

    cross_wallet_balance: Decimal = Decimal("0")
    positions: list[_MarginCallPosition] = field(default_factory=list)


def _parse_order_update(payload: dict) -> OrderUpdate:
    o = payload.get("o", {})
    return OrderUpdate(
        kind=EventKind.ORDER_UPDATE,
        event_time_ms=int(payload.get("E", 0)),
        raw=payload,
        symbol=str(o.get("s", "")),
        client_order_id=str(o.get("c", "")),
        order_id=int(o.get("i", 0)),
        side=str(o.get("S", "")),
        order_type=str(o.get("o", "")),
        time_in_force=str(o.get("f", "")),
        original_quantity=_d(o.get("q")),
        original_price=_d(o.get("p")),
        average_price=_d(o.get("ap")),
        stop_price=_d(o.get("sp")),
        execution_type=str(o.get("x", "")),
        order_status=str(o.get("X", "")),
        filled_quantity=_d(o.get("z")),
        last_filled_quantity=_d(o.get("l")),
        last_filled_price=_d(o.get("L")),
        commission=_d(o.get("n")),
        commission_asset=str(o.get("N", "")),
        realized_pnl=_d(o.get("rp")),
        is_reduce_only=bool(o.get("R", False)),
        is_close_position=bool(o.get("cp", False)),
        is_liquidation=bool(o.get("ot", "")) and str(o.get("ot")).startswith("LIQUIDATION"),
    )


def _parse_account_update(payload: dict) -> AccountUpdate:
    a = payload.get("a", {})
    balances = [
        _AccountUpdateBalance(
            asset=str(b.get("a", "")),
            wallet_balance=_d(b.get("wb")),
            cross_wallet_balance=_d(b.get("cw")),
            balance_change=_d(b.get("bc")),
        )
        for b in a.get("B", [])
    ]
    positions = [
        _AccountUpdatePosition(
            symbol=str(p.get("s", "")),
            quantity=_d(p.get("pa")),
            entry_price=_d(p.get("ep")),
            accumulated_realized=_d(p.get("cr")),
            unrealized_pnl=_d(p.get("up")),
            margin_type=str(p.get("mt", "")),
            isolated_wallet=_d(p.get("iw")),
            position_side=str(p.get("ps", "")),
        )
        for p in a.get("P", [])
    ]
    return AccountUpdate(
        kind=EventKind.ACCOUNT_UPDATE,
        event_time_ms=int(payload.get("E", 0)),
        raw=payload,
        reason=str(a.get("m", "")),
        balances=balances,
        positions=positions,
    )


def _parse_margin_call(payload: dict) -> MarginCallEvent:
    positions = [
        _MarginCallPosition(
            symbol=str(p.get("s", "")),
            position_side=str(p.get("ps", "")),
            quantity=_d(p.get("pa")),
            margin_type=str(p.get("mt", "")),
            isolated_wallet=_d(p.get("iw")),
            mark_price=_d(p.get("mp")),
            unrealized_pnl=_d(p.get("up")),
            maintenance_margin_required=_d(p.get("mm")),
        )
        for p in payload.get("p", [])
    ]
    return MarginCallEvent(
        kind=EventKind.MARGIN_CALL,
        event_time_ms=int(payload.get("E", 0)),
        raw=payload,
        cross_wallet_balance=_d(payload.get("cw")),
        positions=positions,
    )


def parse_event(payload: dict) -> UserDataEvent:
    """Parse a raw user-data WebSocket message into a typed event.

    Unknown event types are returned as a generic :class:`UserDataEvent` with
    ``kind = EventKind.OTHER`` so callers can choose to log them without
    crashing the stream.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"Expected dict payload, got {type(payload).__name__}")

    event = payload.get("e")
    if event == EventKind.ORDER_UPDATE.value:
        return _parse_order_update(payload)
    if event == EventKind.ACCOUNT_UPDATE.value:
        return _parse_account_update(payload)
    if event == EventKind.MARGIN_CALL.value:
        return _parse_margin_call(payload)
    if event == EventKind.ACCOUNT_CONFIG_UPDATE.value:
        return UserDataEvent(
            kind=EventKind.ACCOUNT_CONFIG_UPDATE,
            event_time_ms=int(payload.get("E", 0)),
            raw=payload,
        )
    return UserDataEvent(kind=EventKind.OTHER, event_time_ms=int(payload.get("E", 0)), raw=payload)


EventHandler = Callable[[UserDataEvent], Awaitable[None]]


class UserDataStream:
    """Long-running consumer of the futures user-data WebSocket.

    Usage::

        async with BinanceFuturesClient(...) as client:
            stream = UserDataStream(client)
            await stream.run(my_async_handler)

    The stream auto-reconnects via ``python-binance``'s socket manager. The
    ``run`` coroutine blocks forever; cancel its task to stop.
    """

    def __init__(self, client: BinanceFuturesClient) -> None:
        self._client = client

    async def run(self, handler: EventHandler) -> None:
        bsm = BinanceSocketManager(self._client.client)
        socket = bsm.futures_user_socket()
        backoff = 1.0
        while True:
            try:
                async with socket as ws:
                    backoff = 1.0
                    logger.info("User data stream connected")
                    while True:
                        msg = await ws.recv()
                        if msg is None:
                            continue
                        try:
                            event = parse_event(msg)
                        except Exception:
                            logger.exception("Failed to parse user-data event: %r", msg)
                            continue
                        try:
                            await handler(event)
                        except Exception:
                            logger.exception("Handler raised on event %s", event.kind)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("User data stream error; reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
