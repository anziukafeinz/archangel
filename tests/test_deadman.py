"""Unit tests for the dead-man's switch."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from archangel.safety.deadman import DeadManSwitch, static_symbols


class FakeClient:
    """Minimal fake satisfying the methods DeadManSwitch calls."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._fail_on = fail_on or set()

    async def set_countdown_cancel(self, symbol: str, countdown_ms: int) -> None:
        if symbol in self._fail_on:
            raise RuntimeError(f"injected failure for {symbol}")
        self.calls.append((symbol, countdown_ms))


def make_switch(
    client: FakeClient,
    symbols: list[str],
    *,
    countdown_ms: int = 60_000,
    interval_s: float = 15.0,
) -> DeadManSwitch:
    return DeadManSwitch(
        client,  # type: ignore[arg-type]
        countdown_ms=countdown_ms,
        interval_s=interval_s,
        resolve_symbols=static_symbols(symbols),
    )


@pytest.mark.asyncio
async def test_heartbeat_arms_each_symbol() -> None:
    client = FakeClient()
    switch = make_switch(client, ["btcusdt", "ETHUSDT"])

    targets = await switch.heartbeat_once()

    assert {t.symbol for t in targets} == {"BTCUSDT", "ETHUSDT"}
    assert all(t.countdown_ms == 60_000 for t in targets)
    assert {(s, ms) for s, ms in client.calls} == {
        ("BTCUSDT", 60_000),
        ("ETHUSDT", 60_000),
    }


@pytest.mark.asyncio
async def test_heartbeat_disarms_dropped_symbols() -> None:
    client = FakeClient()
    switch = DeadManSwitch(
        client,  # type: ignore[arg-type]
        countdown_ms=60_000,
        interval_s=15.0,
        resolve_symbols=_changing_symbols([["BTCUSDT", "ETHUSDT"], ["BTCUSDT"]]),
    )

    await switch.heartbeat_once()
    client.calls.clear()
    await switch.heartbeat_once()

    # Second tick: BTCUSDT is rearmed at 60s, ETHUSDT is disarmed (countdown=0)
    assert ("BTCUSDT", 60_000) in client.calls
    assert ("ETHUSDT", 0) in client.calls


@pytest.mark.asyncio
async def test_disarm_all_called_on_run_cancel() -> None:
    import asyncio

    client = FakeClient()
    switch = make_switch(client, ["BTCUSDT"], interval_s=0.01)

    task = asyncio.create_task(switch.run())
    await asyncio.sleep(0.05)  # allow at least one heartbeat
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Final disarm should have sent countdown=0 for BTCUSDT
    assert ("BTCUSDT", 0) in client.calls


@pytest.mark.asyncio
async def test_failed_arm_does_not_break_loop() -> None:
    client = FakeClient(fail_on={"BTCUSDT"})
    switch = make_switch(client, ["BTCUSDT", "ETHUSDT"])

    targets = await switch.heartbeat_once()

    assert {t.symbol for t in targets} == {"ETHUSDT"}
    assert ("ETHUSDT", 60_000) in client.calls
    # BTCUSDT's failed arm should not record a successful call
    assert ("BTCUSDT", 60_000) not in client.calls


def test_countdown_below_min_rejected() -> None:
    with pytest.raises(ValueError, match="countdown_ms must be"):
        DeadManSwitch(
            FakeClient(),  # type: ignore[arg-type]
            countdown_ms=1_000,
            interval_s=0.1,
            resolve_symbols=static_symbols([]),
        )


def test_interval_must_be_strictly_less_than_countdown() -> None:
    with pytest.raises(ValueError, match="strictly less than"):
        DeadManSwitch(
            FakeClient(),  # type: ignore[arg-type]
            countdown_ms=10_000,
            interval_s=10.0,
            resolve_symbols=static_symbols([]),
        )


def test_zero_interval_rejected() -> None:
    with pytest.raises(ValueError, match="interval_s must be > 0"):
        DeadManSwitch(
            FakeClient(),  # type: ignore[arg-type]
            countdown_ms=10_000,
            interval_s=0,
            resolve_symbols=static_symbols([]),
        )


def _changing_symbols(sequence: list[list[str]]) -> Any:
    """Return a resolver that yields the next list each call (last one repeats)."""
    iterator = iter(sequence)
    last = sequence[-1]

    async def _resolve() -> list[str]:
        nonlocal last
        with contextlib.suppress(StopIteration):
            last = next(iterator)
        return last

    return _resolve
