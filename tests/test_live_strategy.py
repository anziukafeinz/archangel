"""Unit tests for LiveStrategyRunner covering dry-run, flip, max-trades, auto-stop."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from archangel.exchange import OrderSide
from archangel.strategy.base import Bar, Signal, SignalAction, Strategy
from archangel.trading.live_strategy import LiveStrategyRunner


@dataclass
class FakeBar:
    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _raw_row(i: int, price: float) -> list:
    """Build the 12-element Binance klines row shape."""
    return [
        i * 60_000,
        str(price),
        str(price + 0.5),
        str(price - 0.5),
        str(price),
        "1",
        (i + 1) * 60_000 - 1,
        "0",
        0,
        "0",
        "0",
        "0",
    ]


class ScriptedClient:
    """Stand-in for BinanceFuturesClient used for get_klines only.

    Each call to :meth:`get_klines` advances a cursor by one **closed** bar
    and appends a synthetic in-progress bar at the tail, so the runner's
    ``rows[:-1]`` drop pattern yields exactly one fresh closed bar per step
    after the first. Tests set ``warmup_bars=0`` so this client only
    services the poll path.
    """

    def __init__(self, rows: list[list]) -> None:
        self._rows = rows
        self._cursor = 0

    async def get_klines(
        self, symbol: str, interval: str, *, start_ms=None, end_ms=None, limit: int = 1500
    ) -> list[list]:
        if self._cursor < len(self._rows):
            self._cursor += 1
        closed = self._rows[max(0, self._cursor - limit) : self._cursor]
        # Append an in-progress bar so rows[:-1] leaves all the closed ones.
        in_progress = _raw_row(self._cursor + 10_000, 0.0)
        return [*closed, in_progress]


class ScriptedStrategy(Strategy):
    """Fires a queued list of signals bar-by-bar."""

    name = "scripted"

    def __init__(self, signals: list[Signal | None]) -> None:
        self._signals = list(signals)
        self._idx = 0

    def on_bar(self, bar: Bar) -> Signal | None:
        if self._idx >= len(self._signals):
            return None
        sig = self._signals[self._idx]
        self._idx += 1
        return sig


@dataclass
class FakeService:
    """Mocked TradingService capturing all interactions."""

    client: ScriptedClient
    plans: list = field(default_factory=list)
    executes: list = field(default_factory=list)
    closes: list = field(default_factory=list)
    risk_violation: str | None = None

    async def plan_trade(self, **kwargs):
        if self.risk_violation is not None:
            from archangel.risk import RiskViolation

            raise RiskViolation(self.risk_violation)
        self.plans.append(kwargs)

        @dataclass
        class _Plan:
            symbol: str
            side: OrderSide
            quantity: Decimal = Decimal("1")
            stop_loss_price: Decimal = Decimal("0")

        return _Plan(symbol=kwargs["symbol"], side=kwargs["side"])

    async def execute_plan(self, plan, *, market_entry: bool = True):
        self.executes.append(plan)
        return ({}, {}, None)

    async def close(self, symbol: str):
        self.closes.append(symbol)
        return {}


def _mk_runner(
    *,
    signals: list[Signal | None],
    bar_count: int = 20,
    dry_run: bool = True,
    max_trades: int | None = None,
    auto_stop_after: float | None = None,
    risk_violation: str | None = None,
) -> tuple[LiveStrategyRunner, FakeService]:
    rows = [_raw_row(i, 100 + i * 0.1) for i in range(bar_count)]
    client = ScriptedClient(rows)
    service = FakeService(client=client, risk_violation=risk_violation)

    runner = LiveStrategyRunner.__new__(LiveStrategyRunner)
    # Manual init so we can inject FakeService without needing full TradingService.
    runner._service = service  # type: ignore[assignment]
    runner._strategy = ScriptedStrategy(signals)
    runner._symbol = "BTCUSDT"
    runner._interval = "1m"
    from archangel.backtest.paper import interval_ms

    runner._interval_ms = interval_ms("1m")
    runner._dry_run = dry_run
    runner._max_trades = max_trades
    runner._auto_stop_after = auto_stop_after
    runner._warmup_bars = 0  # tests skip warmup and drive bars via step()
    runner._now = lambda: 0.0
    runner._started_at = 0.0
    runner._last_closed_ms = 0
    runner._pending = None
    from archangel.trading.live_strategy import RunnerState

    runner.state = RunnerState()
    return runner, service


@pytest.mark.asyncio
async def test_dry_run_does_not_call_execute() -> None:
    entry = Signal(
        action=SignalAction.ENTER_LONG,
        stop_loss=Decimal("95"),
        take_profit=Decimal("105"),
    )
    runner, service = _mk_runner(signals=[entry, None, None], dry_run=True)
    await runner.warmup()
    await runner.step()
    await runner.step()  # second bar -> pending fires
    assert service.executes == []
    assert service.plans == []
    assert runner.state.trades_opened == 1
    decisions = [e.decision for e in runner.state.events]
    assert "dry_run" in decisions


@pytest.mark.asyncio
async def test_live_mode_places_bracket_order() -> None:
    entry = Signal(
        action=SignalAction.ENTER_LONG,
        stop_loss=Decimal("95"),
        take_profit=Decimal("105"),
    )
    runner, service = _mk_runner(signals=[entry, None, None], dry_run=False)
    await runner.warmup()
    await runner.step()
    await runner.step()
    assert len(service.plans) == 1
    assert len(service.executes) == 1
    assert runner.state.position_side == "LONG"


@pytest.mark.asyncio
async def test_flip_closes_existing_before_opening() -> None:
    long_sig = Signal(action=SignalAction.ENTER_LONG, stop_loss=Decimal("95"))
    short_sig = Signal(action=SignalAction.ENTER_SHORT, stop_loss=Decimal("110"))
    runner, service = _mk_runner(signals=[long_sig, short_sig, None, None], dry_run=False)
    await runner.warmup()
    await runner.step()  # bar 0 processed, long pending
    await runner.step()  # bar 1: fire long, queue short
    await runner.step()  # bar 2: flip -> close + short
    # Expect: 1 close, 2 opens (long + short)
    assert len(service.closes) == 1
    assert len(service.executes) == 2
    assert runner.state.position_side == "SHORT"


@pytest.mark.asyncio
async def test_skip_when_already_in_desired_side() -> None:
    long1 = Signal(action=SignalAction.ENTER_LONG, stop_loss=Decimal("95"))
    long2 = Signal(action=SignalAction.ENTER_LONG, stop_loss=Decimal("95"))
    runner, service = _mk_runner(signals=[long1, long2, None], dry_run=True)
    await runner.warmup()
    await runner.step()
    await runner.step()
    await runner.step()
    assert runner.state.trades_opened == 1
    assert any(e.decision == "skipped" for e in runner.state.events)


@pytest.mark.asyncio
async def test_exit_signal_closes_position() -> None:
    long_sig = Signal(action=SignalAction.ENTER_LONG, stop_loss=Decimal("95"))
    exit_sig = Signal(action=SignalAction.EXIT)
    runner, service = _mk_runner(signals=[long_sig, exit_sig, None], dry_run=False)
    await runner.warmup()
    await runner.step()
    await runner.step()
    await runner.step()
    assert len(service.closes) == 1
    assert runner.state.position_side is None


@pytest.mark.asyncio
async def test_max_trades_stops_runner() -> None:
    sigs = [
        Signal(action=SignalAction.ENTER_LONG, stop_loss=Decimal("95")),
        None,
        Signal(action=SignalAction.ENTER_SHORT, stop_loss=Decimal("110")),
        None,
        Signal(action=SignalAction.ENTER_LONG, stop_loss=Decimal("95")),
        None,
    ]
    runner, _service = _mk_runner(signals=sigs, dry_run=True, max_trades=2)
    await runner.warmup()
    for _ in range(6):
        await runner.step()
        if runner.state.stopped:
            break
    assert runner.state.stopped
    assert "max_trades=2" in runner.state.stop_reason


@pytest.mark.asyncio
async def test_auto_stop_after_seconds() -> None:
    runner, _service = _mk_runner(signals=[None, None, None], dry_run=True, auto_stop_after=60.0)
    runner._now = lambda: 120.0  # well past the 60s stop threshold
    runner._started_at = 0.0
    await runner.warmup()
    await runner.step()
    assert runner.state.stopped
    assert "auto_stop_after" in runner.state.stop_reason


@pytest.mark.asyncio
async def test_risk_violation_skips_without_execute() -> None:
    entry = Signal(action=SignalAction.ENTER_LONG, stop_loss=Decimal("95"))
    runner, service = _mk_runner(
        signals=[entry, None, None],
        dry_run=False,
        risk_violation="daily loss limit hit",
    )
    await runner.warmup()
    await runner.step()
    await runner.step()
    assert service.executes == []
    assert runner.state.position_side is None
    assert any("risk guard" in e.reason for e in runner.state.events)


@pytest.mark.asyncio
async def test_no_stop_loss_skipped() -> None:
    entry = Signal(action=SignalAction.ENTER_LONG)  # no stop
    runner, service = _mk_runner(signals=[entry, None, None], dry_run=False)
    await runner.warmup()
    await runner.step()
    await runner.step()
    assert service.plans == []
    assert any(e.decision == "skipped" for e in runner.state.events)


@pytest.mark.asyncio
async def test_shutdown_without_position_is_noop() -> None:
    runner, service = _mk_runner(signals=[None, None], dry_run=False)
    await runner.shutdown(flatten=True)
    assert runner.state.stopped
    assert service.closes == []


@pytest.mark.asyncio
async def test_shutdown_with_dry_run_position_does_not_flatten() -> None:
    long_sig = Signal(action=SignalAction.ENTER_LONG, stop_loss=Decimal("95"))
    runner, service = _mk_runner(signals=[long_sig, None], dry_run=True)
    await runner.warmup()
    await runner.step()
    await runner.step()
    await runner.shutdown(flatten=True)
    assert service.closes == []  # dry-run never hits exchange
