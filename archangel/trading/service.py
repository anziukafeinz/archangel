"""Trading service: glue between exchange client, risk guard, and UI surfaces."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from archangel.config import Settings, get_settings
from archangel.exchange import (
    AccountSnapshot,
    BinanceFuturesClient,
    BracketOrderRequest,
    OrderResult,
    OrderSide,
)
from archangel.risk import RiskGuard, position_size_from_risk

logger = logging.getLogger(__name__)


@dataclass
class TradePlan:
    """Result of pre-trade planning, ready to be executed (or shown to the user)."""

    symbol: str
    side: OrderSide
    quantity: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal | None
    risk_amount: Decimal
    risk_pct: Decimal
    leverage: int


class TradingService:
    """Coordinates risk checks and exchange execution for manual trading."""

    def __init__(
        self,
        client: BinanceFuturesClient,
        risk_guard: RiskGuard,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._risk = risk_guard
        self._settings = settings or get_settings()

    @property
    def client(self) -> BinanceFuturesClient:
        return self._client

    @property
    def risk(self) -> RiskGuard:
        return self._risk

    async def account(self) -> AccountSnapshot:
        return await self._client.get_account_snapshot()

    async def plan_trade(
        self,
        *,
        symbol: str,
        side: OrderSide,
        stop_loss_price: Decimal,
        take_profit_price: Decimal | None = None,
        entry_price: Decimal | None = None,
        risk_pct: Decimal | None = None,
        leverage: int | None = None,
    ) -> TradePlan:
        """Compute a sized trade plan from a stop-loss and risk budget."""
        symbol = symbol.upper()
        snapshot = await self._client.get_account_snapshot()
        allowed, reason = self._risk.should_trade(snapshot)
        if not allowed:
            from archangel.risk import RiskViolation

            raise RiskViolation(reason)

        ref_price = entry_price or await self._client.get_mark_price(symbol)
        filters = await self._client.get_symbol_filters(symbol)
        rpct = risk_pct or Decimal(str(self._settings.risk_per_trade_pct))
        equity = snapshot.total_margin_balance
        qty = position_size_from_risk(
            equity=equity,
            risk_pct=rpct,
            entry_price=ref_price,
            stop_loss_price=stop_loss_price,
            qty_step=filters.qty_step,
            min_qty=filters.min_qty,
        )
        if qty <= 0:
            from archangel.risk import RiskViolation

            raise RiskViolation(
                "Computed position size is below the symbol's minimum quantity. "
                "Either widen the stop, increase risk_pct, or top up the account."
            )

        risk_amount = abs(ref_price - stop_loss_price) * qty
        return TradePlan(
            symbol=symbol,
            side=side,
            quantity=qty,
            entry_price=ref_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            risk_amount=risk_amount,
            risk_pct=rpct,
            leverage=leverage or self._settings.default_leverage,
        )

    async def execute_plan(
        self, plan: TradePlan, *, market_entry: bool = True
    ) -> tuple[OrderResult, OrderResult, OrderResult | None]:
        """Validate against the risk guard and place the bracket order."""
        snapshot = await self._client.get_account_snapshot()
        request = BracketOrderRequest(
            symbol=plan.symbol,
            side=plan.side,
            quantity=plan.quantity,
            entry_price=None if market_entry else plan.entry_price,
            stop_loss_price=plan.stop_loss_price,
            take_profit_price=plan.take_profit_price,
            leverage=plan.leverage,
        )
        # The guard needs a reference price even for market entries.
        validate_request = BracketOrderRequest(
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            entry_price=plan.entry_price,
            stop_loss_price=request.stop_loss_price,
            take_profit_price=request.take_profit_price,
            leverage=request.leverage,
        )
        self._risk.validate_bracket_request(validate_request, snapshot)
        return await self._client.place_bracket_order(request)

    async def close(self, symbol: str) -> OrderResult | None:
        return await self._client.close_position(symbol)

    async def kill_switch(self) -> list[OrderResult]:
        logger.warning("KILL SWITCH engaged — flattening all positions")
        return await self._client.flatten_all()
