"""Live paper trader: runs a Strategy against real-time klines without touching the exchange.

The trader polls the Binance futures klines endpoint on an interval-aligned
clock, parses each newly-closed bar with :func:`parse_kline_row`, feeds it to
the strategy, and simulates fills using the same sizing / fee / slippage
logic as :mod:`archangel.backtest.engine`. Fills and PnL are optionally
pushed to Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from archangel.backtest.engine import (
    BacktestResult,
    Trade,
    _apply_slippage,
    _check_intra_bar_exit,
    _close_position,
    _mark_equity,
    _OpenPosition,
    _size_from_risk,
)
from archangel.exchange.binance import BinanceFuturesClient
from archangel.strategy.base import Bar, Signal, SignalAction, Strategy, parse_kline_row

logger = logging.getLogger(__name__)


INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def interval_ms(interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval {interval!r}")
    return INTERVAL_MS[interval]


class PaperTrader:
    """Long-running paper trader that mirrors the backtester's execution model."""

    def __init__(
        self,
        client: BinanceFuturesClient,
        *,
        symbol: str,
        interval: str,
        strategy: Strategy,
        initial_equity: Decimal = Decimal("10000"),
        risk_per_trade_pct: Decimal = Decimal("1.0"),
        fee_bps: Decimal = Decimal("4"),
        slippage_bps: Decimal = Decimal("2"),
        leverage: Decimal = Decimal("1"),
        warmup_bars: int = 200,
        on_trade: TradeCallback | None = None,
    ) -> None:
        self._client = client
        self._symbol = symbol.upper()
        self._interval = interval
        self._interval_ms = interval_ms(interval)
        self._strategy = strategy
        self._equity = Decimal(str(initial_equity))
        self._initial = self._equity
        self._risk_pct = Decimal(str(risk_per_trade_pct))
        self._fee_bps = Decimal(str(fee_bps))
        self._slip_bps = Decimal(str(slippage_bps))
        self._leverage = Decimal(str(leverage))
        self._warmup_bars = max(warmup_bars, 50)
        self._on_trade = on_trade
        self._position: _OpenPosition | None = None
        self._pending: Signal | None = None
        self._trades: list[Trade] = []
        self._last_closed_ms: int = 0

    @property
    def equity(self) -> Decimal:
        return self._equity

    @property
    def trades(self) -> list[Trade]:
        return list(self._trades)

    async def warmup(self) -> None:
        """Feed the strategy with `warmup_bars` of recent history before going live."""
        rows = await self._client.get_klines(self._symbol, self._interval, limit=self._warmup_bars)
        # drop the last (in-progress) bar
        for row in rows[:-1]:
            bar = parse_kline_row(row)
            self._strategy.on_bar(bar)
            self._last_closed_ms = bar.close_ms

    async def step(self) -> list[Bar]:
        """Fetch any bars that closed since the last step, process them, return them."""
        rows = await self._client.get_klines(self._symbol, self._interval, limit=3)
        closed: list[Bar] = []
        for row in rows[:-1]:  # drop in-progress bar
            bar = parse_kline_row(row)
            if bar.close_ms <= self._last_closed_ms:
                continue
            self._process_bar(bar)
            self._last_closed_ms = bar.close_ms
            closed.append(bar)
        return closed

    async def run(self) -> None:
        """Loop forever, processing one bar per interval."""
        await self.warmup()
        logger.info(
            "Paper trader armed: %s %s strategy=%s equity=%s",
            self._symbol,
            self._interval,
            self._strategy.name,
            self._equity,
        )
        while True:
            await self.step()
            sleep_for = self._sleep_until_next_close()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    def summary(self) -> BacktestResult:
        """Return a BacktestResult-shaped snapshot of the session so far."""
        from archangel.backtest.metrics import compute_metrics

        curve = [
            (t.exit_ms, self._initial + _cumulative(self._trades[: i + 1]))
            for i, t in enumerate(self._trades)
        ]
        if not curve:
            curve = [(int(time.time() * 1000), self._initial)]
        metrics = compute_metrics(
            initial_equity=self._initial,
            final_equity=self._equity,
            trades=self._trades,
            equity_curve=curve,
        )
        return BacktestResult(
            symbol=self._symbol,
            strategy=self._strategy.name,
            initial_equity=self._initial,
            final_equity=self._equity,
            trades=list(self._trades),
            equity_curve=curve,
            metrics=metrics,
        )

    # ---- internals ----------------------------------------------------

    def _sleep_until_next_close(self) -> float:
        now_ms = int(time.time() * 1000)
        next_close = ((now_ms // self._interval_ms) + 1) * self._interval_ms
        return max(0.0, (next_close - now_ms) / 1000.0 + 1.0)

    def _process_bar(self, bar: Bar) -> None:
        # 1. intra-bar exit check
        if self._position is not None:
            exit_info = _check_intra_bar_exit(self._position, bar)
            if exit_info is not None:
                price, reason = exit_info
                trade, self._equity = _close_position(
                    self._position,
                    bar.close_ms,
                    price,
                    reason,
                    self._equity,
                    self._fee_bps,
                )
                self._record_trade(trade)
                self._position = None

        # 2. execute pending signal at this bar's open (simulated)
        if self._pending is not None:
            action = self._pending.action
            exec_price = _apply_slippage(bar.open, action, self._slip_bps)
            if action == SignalAction.EXIT and self._position is not None:
                trade, self._equity = _close_position(
                    self._position,
                    bar.open_ms,
                    exec_price,
                    self._pending.reason or "EXIT",
                    self._equity,
                    self._fee_bps,
                )
                self._record_trade(trade)
                self._position = None
            elif action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
                desired_side = "LONG" if action == SignalAction.ENTER_LONG else "SHORT"
                if self._position is not None and self._position.side != desired_side:
                    trade, self._equity = _close_position(
                        self._position,
                        bar.open_ms,
                        exec_price,
                        "flip",
                        self._equity,
                        self._fee_bps,
                    )
                    self._record_trade(trade)
                    self._position = None
                if self._position is None:
                    qty = _size_from_risk(
                        equity=self._equity,
                        risk_pct=self._risk_pct,
                        entry=exec_price,
                        stop=self._pending.stop_loss,
                        leverage=self._leverage,
                    )
                    if qty > 0:
                        fee = exec_price * qty * self._fee_bps / Decimal("10000")
                        self._equity -= fee
                        self._position = _OpenPosition(
                            side=desired_side,
                            entry_ms=bar.open_ms,
                            entry_price=exec_price,
                            quantity=qty,
                            stop_loss=self._pending.stop_loss,
                            take_profit=self._pending.take_profit,
                            entry_fee=fee,
                            reason_in=self._pending.reason or desired_side,
                        )
            self._pending = None

        # 2b. Same-bar intra-bar exit after a fresh entry.
        if self._position is not None:
            exit_info = _check_intra_bar_exit(self._position, bar)
            if exit_info is not None:
                price, reason = exit_info
                trade, self._equity = _close_position(
                    self._position,
                    bar.close_ms,
                    price,
                    reason,
                    self._equity,
                    self._fee_bps,
                )
                self._record_trade(trade)
                self._position = None

        # 3. feed strategy
        signal = self._strategy.on_bar(bar)
        if signal is not None and signal.action != SignalAction.HOLD:
            self._pending = signal

        # 4. mark-to-market snapshot (consumers can poll .equity)
        self._equity_mark = _mark_equity(self._equity, self._position, bar.close)

    def _record_trade(self, trade: Trade) -> None:
        self._trades.append(trade)
        if self._on_trade is not None:
            try:
                self._on_trade(trade, self._equity)
            except Exception:  # pragma: no cover - callback guard
                logger.exception("on_trade callback raised")


def _cumulative(trades: list[Trade]) -> Decimal:
    return sum((t.pnl for t in trades), Decimal("0"))


TradeCallback = "callable[[Trade, Decimal], None]"
