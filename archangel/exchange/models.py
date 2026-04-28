"""Domain models used across the exchange layer."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> OrderSide:
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


@dataclass(frozen=True)
class SymbolFilters:
    """Trading filters for a Binance Futures symbol (USDT-M)."""

    symbol: str
    price_tick: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str  # LONG / SHORT / BOTH
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    liquidation_price: Decimal | None


@dataclass(frozen=True)
class AccountSnapshot:
    total_wallet_balance: Decimal
    total_margin_balance: Decimal
    available_balance: Decimal
    total_unrealized_pnl: Decimal
    positions: list[Position]


@dataclass(frozen=True)
class BracketOrderRequest:
    """Atomic entry + stop-loss (+ optional take-profit) request."""

    symbol: str
    side: OrderSide
    quantity: Decimal
    entry_price: Decimal | None  # None => market entry
    stop_loss_price: Decimal
    take_profit_price: Decimal | None = None
    leverage: int | None = None
    reduce_only_exits: bool = True


@dataclass(frozen=True)
class OrderResult:
    order_id: int
    client_order_id: str
    symbol: str
    side: str
    type: str
    status: str
    quantity: Decimal
    price: Decimal | None
    raw: dict
