"""Dead-man's switch for Binance USDT-M Futures.

Binance exposes ``POST /fapi/v1/countdownCancelAll`` (per symbol). If no
follow-up call arrives within ``countdownTime`` ms, the exchange cancels
every open order on the symbol. :class:`DeadManSwitch` runs a heartbeat that
keeps the timer armed while Archangel is healthy and disables it on a clean
shutdown.

Why not on close everything? Because cancelling resting orders does **not**
flatten existing positions. The watchdog only protects you from leftover
stop-losses or limit ladders if the bot crashes — your live position keeps
its own stop-loss in place. For a true kill-switch on positions, use
``archangel flatten``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from archangel.exchange.binance import BinanceFuturesClient

logger = logging.getLogger(__name__)


ResolveSymbols = Callable[[], Awaitable[list[str]]]


@dataclass(frozen=True)
class DeadManTarget:
    symbol: str
    countdown_ms: int


class DeadManSwitch:
    """Periodically arm Binance's per-symbol countdown cancel.

    Parameters
    ----------
    client:
        Connected :class:`BinanceFuturesClient`.
    countdown_ms:
        Time the exchange will wait before cancelling open orders, in
        milliseconds. Values below the minimum sensible threshold (5s) are
        rejected up front because anything tighter will race the heartbeat.
    interval_s:
        How often the heartbeat fires. Must be **strictly less than**
        ``countdown_ms / 1000``; a 2x safety factor is recommended.
    resolve_symbols:
        Callable returning the list of symbols to protect. Usually either a
        static list or a closure over ``client.get_account_snapshot()`` to
        auto-track open positions.
    """

    MIN_COUNTDOWN_MS = 5_000

    def __init__(
        self,
        client: BinanceFuturesClient,
        *,
        countdown_ms: int,
        interval_s: float,
        resolve_symbols: ResolveSymbols,
    ) -> None:
        if countdown_ms < self.MIN_COUNTDOWN_MS:
            raise ValueError(
                f"countdown_ms must be >= {self.MIN_COUNTDOWN_MS} (got {countdown_ms})"
            )
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        if interval_s * 1000 >= countdown_ms:
            raise ValueError(
                f"interval_s ({interval_s}s) must be strictly less than "
                f"countdown_ms/1000 ({countdown_ms / 1000}s) — recommend 2x safety factor"
            )
        self._client = client
        self._countdown_ms = countdown_ms
        self._interval_s = interval_s
        self._resolve = resolve_symbols
        self._armed: set[str] = set()

    async def heartbeat_once(self) -> list[DeadManTarget]:
        """Run a single heartbeat tick. Returns the targets armed this tick."""
        symbols = await self._resolve()
        symbols_set = {s.upper() for s in symbols}
        targets: list[DeadManTarget] = []

        for symbol in symbols_set:
            try:
                await self._client.set_countdown_cancel(symbol, self._countdown_ms)
                targets.append(DeadManTarget(symbol=symbol, countdown_ms=self._countdown_ms))
                self._armed.add(symbol)
            except Exception:
                logger.exception("Dead-man heartbeat failed for %s", symbol)

        # Disarm symbols we used to track but no longer should
        stale = self._armed - symbols_set
        for symbol in stale:
            try:
                await self._client.set_countdown_cancel(symbol, 0)
            except Exception:
                logger.exception("Dead-man disarm failed for %s", symbol)
            self._armed.discard(symbol)

        return targets

    async def run(
        self,
        on_tick: Callable[[list[DeadManTarget]], Awaitable[None]] | None = None,
    ) -> None:
        """Run the heartbeat loop until cancelled. Disarms all on exit."""
        try:
            while True:
                targets = await self.heartbeat_once()
                if on_tick is not None:
                    try:
                        await on_tick(targets)
                    except Exception:
                        logger.exception("on_tick callback raised")
                await asyncio.sleep(self._interval_s)
        finally:
            await self._disarm_all()

    async def _disarm_all(self) -> None:
        """Send countdown=0 for every armed symbol so the timer is released."""
        for symbol in list(self._armed):
            try:
                await self._client.set_countdown_cancel(symbol, 0)
                logger.info("Dead-man disarmed for %s", symbol)
            except Exception:
                logger.exception("Dead-man final disarm failed for %s", symbol)
        self._armed.clear()


def static_symbols(symbols: list[str]) -> ResolveSymbols:
    """Resolver that always returns a fixed list of symbols."""
    cleaned = [s.strip().upper() for s in symbols if s.strip()]

    async def _resolve() -> list[str]:
        return cleaned

    return _resolve


def open_position_symbols(client: BinanceFuturesClient) -> ResolveSymbols:
    """Resolver that returns symbols with a non-zero open position right now."""

    async def _resolve() -> list[str]:
        snapshot = await client.get_account_snapshot()
        return [p.symbol for p in snapshot.positions if p.quantity != 0]

    return _resolve
