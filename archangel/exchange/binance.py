"""Async Binance USDT-M Futures client wrapper.

Wraps `python-binance`'s `AsyncClient` and exposes a small, opinionated surface
focused on the operations Archangel needs (account snapshot, bracket orders,
position management). All numeric inputs/outputs use ``Decimal`` to avoid the
float rounding problems that have killed real trading bots.
"""

from __future__ import annotations

import logging
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any

from binance import AsyncClient
from binance.enums import (
    FUTURE_ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_MARKET,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
    SIDE_BUY,
    SIDE_SELL,
    TIME_IN_FORCE_GTC,
)
from binance.exceptions import BinanceAPIException

from archangel.exchange.models import (
    AccountSnapshot,
    BracketOrderRequest,
    OrderResult,
    OrderSide,
    Position,
    SymbolFilters,
)

logger = logging.getLogger(__name__)


def _d(value: Any) -> Decimal:
    """Coerce Binance string/float fields into Decimal."""
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _round_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` down to the nearest multiple of ``step``."""
    if step <= 0:
        return value
    return (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


class BinanceFuturesClient:
    """Thin async wrapper over python-binance for USDT-M futures."""

    def __init__(self, api_key: str, api_secret: str, *, testnet: bool = True) -> None:
        if not api_key or not api_secret:
            raise ValueError("Binance API key and secret are required")
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._client: AsyncClient | None = None
        self._symbol_filters: dict[str, SymbolFilters] = {}

    async def __aenter__(self) -> BinanceFuturesClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._client is None:
            self._client = await AsyncClient.create(
                api_key=self._api_key,
                api_secret=self._api_secret,
                testnet=self._testnet,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close_connection()
            self._client = None

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("Client not connected. Call .connect() or use 'async with'.")
        return self._client

    # ---- Market data ---------------------------------------------------

    async def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        symbol = symbol.upper()
        if symbol in self._symbol_filters:
            return self._symbol_filters[symbol]

        info = await self.client.futures_exchange_info()
        for sym in info["symbols"]:
            if sym["symbol"] != symbol:
                continue
            tick = qty_step = min_qty = min_notional = Decimal("0")
            for f in sym["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    tick = _d(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    qty_step = _d(f["stepSize"])
                    min_qty = _d(f["minQty"])
                elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = _d(f.get("notional") or f.get("minNotional") or 0)
            filters = SymbolFilters(
                symbol=symbol,
                price_tick=tick,
                qty_step=qty_step,
                min_qty=min_qty,
                min_notional=min_notional,
            )
            self._symbol_filters[symbol] = filters
            return filters
        raise ValueError(f"Symbol {symbol} not found on Binance Futures")

    async def get_mark_price(self, symbol: str) -> Decimal:
        data = await self.client.futures_mark_price(symbol=symbol.upper())
        return _d(data["markPrice"])

    # ---- Account ------------------------------------------------------

    async def get_account_snapshot(self) -> AccountSnapshot:
        info = await self.client.futures_account()
        positions: list[Position] = []
        for p in info.get("positions", []):
            qty = _d(p.get("positionAmt"))
            if qty == 0:
                continue
            positions.append(
                Position(
                    symbol=p["symbol"],
                    side=p.get("positionSide", "BOTH"),
                    quantity=qty,
                    entry_price=_d(p.get("entryPrice")),
                    mark_price=_d(p.get("markPrice") or p.get("entryPrice")),
                    unrealized_pnl=_d(p.get("unRealizedProfit")),
                    leverage=int(_d(p.get("leverage")) or 1),
                    liquidation_price=_d(p.get("liquidationPrice")) or None,
                )
            )
        return AccountSnapshot(
            total_wallet_balance=_d(info.get("totalWalletBalance")),
            total_margin_balance=_d(info.get("totalMarginBalance")),
            available_balance=_d(info.get("availableBalance")),
            total_unrealized_pnl=_d(info.get("totalUnrealizedProfit")),
            positions=positions,
        )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            await self.client.futures_change_leverage(symbol=symbol.upper(), leverage=leverage)
        except BinanceAPIException as e:
            # -4046: no need to change leverage. Treat as success.
            if getattr(e, "code", None) == -4046:
                return
            raise

    async def set_countdown_cancel(self, symbol: str, countdown_ms: int) -> None:
        """Arm Binance's dead-man's switch for ``symbol``.

        Binance auto-cancels every open order on the symbol if no follow-up
        ping arrives within ``countdown_ms`` milliseconds. Pass ``0`` to
        disable an active timer.

        Endpoint: ``POST /fapi/v1/countdownCancelAll``.
        """
        await self.client.futures_countdown_cancel_all(
            symbol=symbol.upper(), countdownTime=int(countdown_ms)
        )

    # ---- Orders -------------------------------------------------------

    @staticmethod
    def _make_client_order_id(prefix: str = "arc") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _to_result(payload: dict, *, fallback_symbol: str = "") -> OrderResult:
        return OrderResult(
            order_id=int(payload.get("orderId", 0)),
            client_order_id=str(payload.get("clientOrderId", "")),
            symbol=str(payload.get("symbol", fallback_symbol)),
            side=str(payload.get("side", "")),
            type=str(payload.get("type", "")),
            status=str(payload.get("status", "")),
            quantity=_d(payload.get("origQty")),
            price=_d(payload.get("price")) or None,
            raw=payload,
        )

    async def place_bracket_order(
        self, request: BracketOrderRequest
    ) -> tuple[OrderResult, OrderResult, OrderResult | None]:
        """Place entry + stop-loss + optional take-profit.

        Returns ``(entry, stop_loss, take_profit_or_none)``. If the entry order
        is rejected, neither protection order is sent. If the stop-loss order
        is rejected after a successful entry, the entry is closed immediately
        with a reduce-only market order to avoid leaving an unprotected
        position.
        """
        symbol = request.symbol.upper()
        filters = await self.get_symbol_filters(symbol)
        qty = _round_step(request.quantity, filters.qty_step)
        if qty < filters.min_qty or qty <= 0:
            raise ValueError(
                f"Quantity {request.quantity} is below min_qty={filters.min_qty} for {symbol}"
            )

        if request.leverage is not None:
            await self.set_leverage(symbol, request.leverage)

        entry_side = SIDE_BUY if request.side is OrderSide.BUY else SIDE_SELL
        exit_side = SIDE_SELL if request.side is OrderSide.BUY else SIDE_BUY
        qty_str = format(qty, "f")

        entry_kwargs: dict[str, Any] = {
            "symbol": symbol,
            "side": entry_side,
            "quantity": qty_str,
            "newClientOrderId": self._make_client_order_id("arc-en"),
        }
        if request.entry_price is None:
            entry_kwargs["type"] = FUTURE_ORDER_TYPE_MARKET
        else:
            entry_price = _round_step(request.entry_price, filters.price_tick)
            entry_kwargs.update(
                {
                    "type": FUTURE_ORDER_TYPE_LIMIT,
                    "price": format(entry_price, "f"),
                    "timeInForce": TIME_IN_FORCE_GTC,
                }
            )

        entry_payload = await self.client.futures_create_order(**entry_kwargs)
        entry_result = self._to_result(entry_payload, fallback_symbol=symbol)

        stop_price = _round_step(request.stop_loss_price, filters.price_tick)
        sl_kwargs: dict[str, Any] = {
            "symbol": symbol,
            "side": exit_side,
            "type": FUTURE_ORDER_TYPE_STOP_MARKET,
            "stopPrice": format(stop_price, "f"),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "newClientOrderId": self._make_client_order_id("arc-sl"),
        }
        try:
            sl_payload = await self.client.futures_create_order(**sl_kwargs)
        except BinanceAPIException:
            logger.exception(
                "Stop-loss order rejected after entry; flattening %s to avoid naked exposure",
                symbol,
            )
            await self._flatten_symbol(symbol, exit_side, qty_str)
            raise

        sl_result = self._to_result(sl_payload, fallback_symbol=symbol)

        tp_result: OrderResult | None = None
        if request.take_profit_price is not None:
            tp_price = _round_step(request.take_profit_price, filters.price_tick)
            tp_payload = await self.client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice=format(tp_price, "f"),
                closePosition="true",
                workingType="MARK_PRICE",
                newClientOrderId=self._make_client_order_id("arc-tp"),
            )
            tp_result = self._to_result(tp_payload, fallback_symbol=symbol)

        return entry_result, sl_result, tp_result

    async def _flatten_symbol(self, symbol: str, exit_side: str, qty_str: str) -> None:
        await self.client.futures_create_order(
            symbol=symbol,
            side=exit_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=qty_str,
            reduceOnly="true",
            newClientOrderId=self._make_client_order_id("arc-fl"),
        )

    async def close_position(self, symbol: str) -> OrderResult | None:
        """Close any open position on ``symbol`` with a reduce-only market order."""
        symbol = symbol.upper()
        snapshot = await self.get_account_snapshot()
        for pos in snapshot.positions:
            if pos.symbol != symbol:
                continue
            qty = abs(pos.quantity)
            exit_side = SIDE_SELL if pos.quantity > 0 else SIDE_BUY
            payload = await self.client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=format(qty, "f"),
                reduceOnly="true",
                newClientOrderId=self._make_client_order_id("arc-cl"),
            )
            await self.cancel_open_orders(symbol)
            return self._to_result(payload, fallback_symbol=symbol)
        return None

    async def cancel_open_orders(self, symbol: str) -> None:
        try:
            await self.client.futures_cancel_all_open_orders(symbol=symbol.upper())
        except BinanceAPIException:
            logger.exception("Failed to cancel open orders for %s", symbol)

    async def flatten_all(self) -> list[OrderResult]:
        """Kill switch: close every open position and cancel every working order."""
        snapshot = await self.get_account_snapshot()
        results: list[OrderResult] = []
        for pos in snapshot.positions:
            res = await self.close_position(pos.symbol)
            if res is not None:
                results.append(res)
        return results
