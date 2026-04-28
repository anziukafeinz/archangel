"""Binance USDT-M Futures **liquidation feed** consumer.

Subscribes to the public ``!forceOrder@arr`` stream (all perpetuals) or a
single-symbol ``<symbol>@forceOrder`` stream and parses each payload into a
typed :class:`LiquidationEvent`.

Unlike the authenticated user-data stream, no listenKey is required — these
are market-wide forced-order events. Use them to:

- watch where the market is capitulating right now
- gauge squeeze risk before entering a position
- build leaderboards of the largest liquidations over a session

The parser is a pure function (``parse_liquidation``) so it is fully
unit-testable without any network access.

Reference: https://binance-docs.github.io/apidocs/futures/en/#liquidation-order-streams
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from binance import BinanceSocketManager

from archangel.exchange.binance import BinanceFuturesClient

logger = logging.getLogger(__name__)


def _d(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class LiquidationEvent:
    """A parsed ``forceOrder`` event.

    Binance publishes liquidations in the same shape whether they originate
    from the all-market stream or a single-symbol stream. Fields mirror the
    exchange's short keys but are unpacked for readability.
    """

    event_time_ms: int
    symbol: str
    side: str  # BUY / SELL — side that exchange forcibly placed to flatten
    order_type: str  # LIMIT / MARKET / ...
    time_in_force: str  # IOC / GTC / ...
    original_quantity: Decimal
    price: Decimal
    average_price: Decimal
    order_status: str  # FILLED / PARTIALLY_FILLED / ...
    last_filled_quantity: Decimal
    filled_accumulated_quantity: Decimal
    trade_time_ms: int
    raw: dict

    @property
    def notional_usd(self) -> Decimal:
        """USD value of the filled portion (avg price × filled qty).

        Falls back to order price when average price is missing (e.g. a new
        unfilled liquidation order).
        """
        price = self.average_price if self.average_price > 0 else self.price
        qty = (
            self.filled_accumulated_quantity
            if self.filled_accumulated_quantity > 0
            else self.original_quantity
        )
        return (price * qty).quantize(Decimal("0.01"))

    @property
    def flattened_side(self) -> str:
        """Side of the position that was liquidated.

        The exchange places a SELL to flatten a long, and a BUY to flatten a
        short, so the position side is the inverse of the order side.
        """
        if self.side.upper() == "SELL":
            return "LONG"
        if self.side.upper() == "BUY":
            return "SHORT"
        return "?"


def parse_liquidation(payload: dict | str | bytes) -> LiquidationEvent:
    """Convert a raw ``forceOrder`` payload into a :class:`LiquidationEvent`.

    Accepts either a parsed dict or a raw JSON string/bytes straight off the
    socket. Handles both the direct payload (``{"e":"forceOrder", "o":{...}}``)
    and the wrapped multiplex payload (``{"stream":"...", "data":{...}}``).
    """
    if isinstance(payload, bytes | bytearray):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected dict payload, got {type(payload).__name__}")

    if "data" in payload and isinstance(payload["data"], dict):
        payload = payload["data"]

    if payload.get("e") != "forceOrder":
        raise ValueError(f"Not a forceOrder event: e={payload.get('e')!r}")

    if "o" not in payload or not isinstance(payload["o"], dict):
        raise ValueError("forceOrder payload missing 'o' object")
    o = payload["o"]

    return LiquidationEvent(
        event_time_ms=int(payload.get("E", 0) or 0),
        symbol=str(o.get("s", "")),
        side=str(o.get("S", "")),
        order_type=str(o.get("o", "")),
        time_in_force=str(o.get("f", "")),
        original_quantity=_d(o.get("q")),
        price=_d(o.get("p")),
        average_price=_d(o.get("ap")),
        order_status=str(o.get("X", "")),
        last_filled_quantity=_d(o.get("l")),
        filled_accumulated_quantity=_d(o.get("z")),
        trade_time_ms=int(o.get("T", 0) or 0),
        raw=payload,
    )


def format_liquidation(event: LiquidationEvent) -> str:
    """Human-readable single line for logs / Telegram messages."""
    arrow = "🟥" if event.flattened_side == "LONG" else "🟩"
    side_label = f"{event.flattened_side} liquidated"
    qty = event.filled_accumulated_quantity or event.original_quantity
    price = event.average_price if event.average_price > 0 else event.price
    return f"{arrow} *{event.symbol}* {side_label}: {qty} @ {price} (~${event.notional_usd:,.0f})"


LiquidationHandler = Callable[[LiquidationEvent], Awaitable[None]]


class LiquidationStream:
    """Long-running consumer of the public liquidation WebSocket.

    Usage::

        async with BinanceFuturesClient(...) as client:
            stream = LiquidationStream(client, symbols=["BTCUSDT"])
            await stream.run(my_async_handler)

    With ``symbols=None`` (the default) subscribes to ``!forceOrder@arr`` —
    every USDT-M perpetual. Pass a list of symbols to filter on a subset via
    the per-symbol stream.
    """

    def __init__(
        self,
        client: BinanceFuturesClient,
        *,
        symbols: list[str] | None = None,
    ) -> None:
        self._client = client
        self._symbols = [s.upper() for s in symbols] if symbols else None

    def _stream_names(self) -> list[str]:
        if self._symbols:
            return [f"{s.lower()}@forceOrder" for s in self._symbols]
        return ["!forceOrder@arr"]

    async def run(self, handler: LiquidationHandler) -> None:
        bsm = BinanceSocketManager(self._client.client)
        socket = bsm.futures_multiplex_socket(self._stream_names())
        backoff = 1.0
        while True:
            try:
                async with socket as ws:
                    backoff = 1.0
                    logger.info("Liquidation stream connected: %s", self._stream_names())
                    while True:
                        msg = await ws.recv()
                        if msg is None:
                            continue
                        try:
                            event = parse_liquidation(msg)
                        except Exception:
                            logger.exception("Failed to parse liquidation event: %r", msg)
                            continue
                        try:
                            await handler(event)
                        except Exception:
                            logger.exception("Handler raised on liquidation %s", event.symbol)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Liquidation stream error; reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
