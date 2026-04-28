"""Pluggable strategy framework.

Each strategy is a subclass of :class:`Strategy` that returns a :class:`Signal`
(or ``None``) for each new :class:`Bar`. The same strategy code is shared
between the :mod:`archangel.backtest.engine` and the live paper trader, so a
passing backtest is meaningful for the live runner.
"""

from archangel.strategy.base import Bar, Signal, SignalAction, Strategy, parse_kline_row
from archangel.strategy.ema_cross import EmaCrossStrategy
from archangel.strategy.indicators import atr, ema, rolling_mean, rsi, sma
from archangel.strategy.registry import STRATEGIES, get_strategy
from archangel.strategy.rsi_reversion import RsiReversionStrategy

__all__ = [
    "STRATEGIES",
    "Bar",
    "EmaCrossStrategy",
    "RsiReversionStrategy",
    "Signal",
    "SignalAction",
    "Strategy",
    "atr",
    "ema",
    "get_strategy",
    "parse_kline_row",
    "rolling_mean",
    "rsi",
    "sma",
]
