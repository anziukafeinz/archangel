"""Name → Strategy-factory registry used by the CLI."""

from __future__ import annotations

from collections.abc import Callable

from archangel.strategy.base import Strategy
from archangel.strategy.ema_cross import EmaCrossStrategy
from archangel.strategy.rsi_reversion import RsiReversionStrategy

StrategyFactory = Callable[[], Strategy]

STRATEGIES: dict[str, StrategyFactory] = {
    EmaCrossStrategy.name: EmaCrossStrategy,
    RsiReversionStrategy.name: RsiReversionStrategy,
}


def get_strategy(name: str) -> Strategy:
    """Build a strategy instance from its registered name."""
    key = name.strip().lower()
    if key not in STRATEGIES:
        available = ", ".join(sorted(STRATEGIES))
        raise KeyError(f"Unknown strategy {name!r}. Available: {available}")
    return STRATEGIES[key]()
