"""Bridge from strategy signals to real bracket orders.

A :class:`LiveStrategyRunner` takes the exact same :class:`Strategy` instance
that was used for backtesting / paper trading and wires it to the existing
:class:`~archangel.trading.service.TradingService`, so every live order
passes through the :class:`~archangel.risk.RiskGuard` pre-trade checks.

Safety defaults:

- **dry-run is the default**; callers must explicitly set ``dry_run=False``
  to hit the exchange.
- A ``max_trades`` cap stops the runner after N round-trips.
- An ``auto_stop_after_seconds`` wall-clock timer stops the runner
  unconditionally regardless of trade count.
- A ``Ctrl+C`` in the CLI triggers a best-effort close of any position
  opened by the runner (via :meth:`shutdown`).

The runner does **not** maintain its own mark-to-market or intra-bar SL/TP
logic: stop-loss and take-profit are set on the exchange by the bracket
order, so they are enforced server-side regardless of the runner's
liveness. The runner only tracks position state for flip / exit decisions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from archangel.backtest.paper import interval_ms
from archangel.exchange import OrderSide
from archangel.exchange.binance import BinanceFuturesClient
from archangel.risk import RiskViolation
from archangel.strategy.base import Bar, Signal, SignalAction, Strategy, parse_kline_row
from archangel.trading.service import TradingService

logger = logging.getLogger(__name__)


@dataclass
class LiveSignalEvent:
    """Structured record of a signal the runner acted on (or declined)."""

    bar: Bar
    signal: Signal
    decision: str  # "placed" / "dry_run" / "flipped" / "exited" / "skipped"
    reason: str = ""


@dataclass
class RunnerState:
    """Mutable state the runner exposes to tests and CLI status prints."""

    position_side: str | None = None  # "LONG" / "SHORT" / None
    trades_opened: int = 0
    trades_closed: int = 0
    events: list[LiveSignalEvent] = field(default_factory=list)
    stopped: bool = False
    stop_reason: str = ""


class LiveStrategyRunner:
    """Run a :class:`Strategy` against live klines and execute via TradingService."""

    def __init__(
        self,
        *,
        service: TradingService,
        strategy: Strategy,
        symbol: str,
        interval: str,
        dry_run: bool = True,
        max_trades: int | None = None,
        auto_stop_after_seconds: float | None = None,
        warmup_bars: int = 200,
        poll_now_fn=None,  # injectable for tests; defaults to time.time
    ) -> None:
        self._service = service
        self._strategy = strategy
        self._symbol = symbol.upper()
        self._interval = interval
        self._interval_ms = interval_ms(interval)
        self._dry_run = dry_run
        self._max_trades = max_trades
        self._auto_stop_after = auto_stop_after_seconds
        self._warmup_bars = warmup_bars
        self._now = poll_now_fn or time.time
        self._started_at: float | None = None
        self._last_closed_ms: int = 0
        self._pending: Signal | None = None
        self.state = RunnerState()

    # ---- lifecycle ----------------------------------------------------

    @property
    def client(self) -> BinanceFuturesClient:
        return self._service.client

    async def warmup(self) -> None:
        """Seed the strategy with recent history before going live.

        No-op when ``warmup_bars`` is 0 — handy for tests and for strategies
        that carry their own warm-up externally.
        """
        if self._warmup_bars <= 0:
            return
        rows = await self.client.get_klines(self._symbol, self._interval, limit=self._warmup_bars)
        for row in rows[:-1]:  # drop in-progress bar
            bar = parse_kline_row(row)
            self._strategy.on_bar(bar)
            self._last_closed_ms = bar.close_ms
        logger.info(
            "Warm-up complete: %d bars, strategy=%s symbol=%s interval=%s",
            len(rows) - 1,
            self._strategy.name,
            self._symbol,
            self._interval,
        )

    async def step(self) -> list[Bar]:
        """Process any newly-closed bars; returns the list of bars processed."""
        if self.state.stopped:
            return []
        rows = await self.client.get_klines(self._symbol, self._interval, limit=3)
        closed: list[Bar] = []
        for row in rows[:-1]:
            bar = parse_kline_row(row)
            if bar.close_ms <= self._last_closed_ms:
                continue
            await self._on_bar(bar)
            self._last_closed_ms = bar.close_ms
            closed.append(bar)
            if self._should_auto_stop():
                break
        return closed

    async def run(self) -> None:
        """Main loop: warm-up, then process one bar per interval until stopped."""
        self._started_at = self._now()
        await self.warmup()
        logger.warning(
            "Live strategy runner armed: %s %s strategy=%s dry_run=%s "
            "max_trades=%s auto_stop_after=%ss",
            self._symbol,
            self._interval,
            self._strategy.name,
            self._dry_run,
            self._max_trades,
            self._auto_stop_after,
        )
        while not self.state.stopped:
            await self.step()
            if self.state.stopped:
                break
            sleep_for = self._sleep_until_next_close()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    async def shutdown(self, *, flatten: bool = True) -> None:
        """Best-effort cleanup. If ``flatten=True`` and the runner thinks it has
        a live position, attempt to close it via the trading service."""
        self._mark_stopped("shutdown requested")
        if flatten and self.state.position_side is not None and not self._dry_run:
            try:
                await self._service.close(self._symbol)
                logger.warning("Runner flatten on shutdown: %s", self._symbol)
            except Exception:  # pragma: no cover - network/runtime guard
                logger.exception("Flatten on shutdown failed")
        self.state.position_side = None

    # ---- core decision logic -----------------------------------------

    async def _on_bar(self, bar: Bar) -> None:
        # 1. Execute any pending signal from the previous bar first.
        if self._pending is not None:
            await self._execute_signal(bar, self._pending)
            self._pending = None
            if self.state.stopped:
                return

        # 2. Feed the fresh bar into the strategy; defer execution to next bar.
        sig = self._strategy.on_bar(bar)
        if sig is not None and sig.action != SignalAction.HOLD:
            self._pending = sig

    async def _execute_signal(self, bar: Bar, signal: Signal) -> None:
        action = signal.action
        if action == SignalAction.EXIT:
            await self._close_if_open(bar, signal, reason="EXIT signal")
            return

        if action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            return

        desired = "LONG" if action == SignalAction.ENTER_LONG else "SHORT"

        # Flip: close the opposite side first, then fall through to open.
        if self.state.position_side is not None and self.state.position_side != desired:
            await self._close_if_open(bar, signal, reason="flip")
            if self.state.stopped:
                return

        if self.state.position_side == desired:
            # Already in the desired direction; don't double up.
            self._record(bar, signal, "skipped", "already in desired direction")
            return

        if signal.stop_loss is None:
            # RiskGuard would reject this anyway; fail fast with a clear note.
            self._record(bar, signal, "skipped", "strategy produced no stop-loss")
            return

        if self._dry_run:
            self._record(bar, signal, "dry_run", f"would enter {desired}")
            self.state.position_side = desired
            self.state.trades_opened += 1
            self._maybe_stop_on_trade_count("dry-run trade")
            return

        try:
            plan = await self._service.plan_trade(
                symbol=self._symbol,
                side=OrderSide.BUY if desired == "LONG" else OrderSide.SELL,
                stop_loss_price=Decimal(str(signal.stop_loss)),
                take_profit_price=(
                    None if signal.take_profit is None else Decimal(str(signal.take_profit))
                ),
            )
        except RiskViolation as exc:
            self._record(bar, signal, "skipped", f"risk guard: {exc}")
            return

        try:
            await self._service.execute_plan(plan, market_entry=True)
        except Exception as exc:  # pragma: no cover - network/runtime guard
            logger.exception("Bracket order placement failed")
            self._record(bar, signal, "skipped", f"exchange error: {exc}")
            return

        self.state.position_side = desired
        self.state.trades_opened += 1
        self._record(bar, signal, "placed", f"entered {desired} qty={plan.quantity}")
        self._maybe_stop_on_trade_count("live trade")

    async def _close_if_open(self, bar: Bar, signal: Signal, *, reason: str) -> None:
        if self.state.position_side is None:
            return
        if self._dry_run:
            self._record(bar, signal, "exited", f"dry-run close ({reason})")
        else:
            try:
                await self._service.close(self._symbol)
            except Exception as exc:  # pragma: no cover - network/runtime guard
                logger.exception("Close failed")
                self._record(bar, signal, "skipped", f"close error: {exc}")
                return
            self._record(bar, signal, "exited", f"live close ({reason})")
        self.state.position_side = None
        self.state.trades_closed += 1
        self._maybe_stop_on_trade_count(f"close ({reason})")

    # ---- stop conditions ---------------------------------------------

    def _maybe_stop_on_trade_count(self, trigger: str) -> None:
        if self._max_trades is None:
            return
        if self.state.trades_opened >= self._max_trades:
            self._mark_stopped(f"max_trades={self._max_trades} reached (trigger: {trigger})")

    def _should_auto_stop(self) -> bool:
        if self._auto_stop_after is None or self._started_at is None:
            return False
        if self._now() - self._started_at >= self._auto_stop_after:
            self._mark_stopped(f"auto_stop_after={self._auto_stop_after}s elapsed")
            return True
        return False

    def _mark_stopped(self, reason: str) -> None:
        if self.state.stopped:
            return
        self.state.stopped = True
        self.state.stop_reason = reason
        logger.warning("Live runner stopping: %s", reason)

    def _record(self, bar: Bar, signal: Signal, decision: str, reason: str) -> None:
        self.state.events.append(
            LiveSignalEvent(bar=bar, signal=signal, decision=decision, reason=reason)
        )
        logger.warning(
            "%s decision=%s signal=%s bar_close=%s reason=%s",
            self._symbol,
            decision,
            signal.action,
            bar.close,
            reason,
        )

    # ---- sleep helpers -----------------------------------------------

    def _sleep_until_next_close(self) -> float:
        now_ms = int(self._now() * 1000)
        next_close = ((now_ms // self._interval_ms) + 1) * self._interval_ms
        return max(0.0, (next_close - now_ms) / 1000.0 + 1.0)
