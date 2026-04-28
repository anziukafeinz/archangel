"""RSI mean-reversion strategy.

Fades extreme RSI readings: long when RSI ≤ ``oversold`` (default 30),
short when RSI ≥ ``overbought`` (default 70). Exit signals fire when RSI
reverts through a neutral band (default 45–55), which works together with
the backtester's intra-bar stop-loss evaluation.
"""

from __future__ import annotations

from decimal import Decimal

from archangel.strategy.base import Bar, Signal, SignalAction, Strategy
from archangel.strategy.indicators import atr, rsi


class RsiReversionStrategy(Strategy):
    name = "rsi_reversion"

    def __init__(
        self,
        *,
        period: int = 14,
        oversold: Decimal = Decimal("30"),
        overbought: Decimal = Decimal("70"),
        neutral_low: Decimal = Decimal("45"),
        neutral_high: Decimal = Decimal("55"),
        atr_period: int = 14,
        atr_stop_mult: Decimal = Decimal("2"),
    ) -> None:
        if oversold >= overbought:
            raise ValueError("oversold must be < overbought")
        self._period = period
        self._os = Decimal(str(oversold))
        self._ob = Decimal(str(overbought))
        self._nl = Decimal(str(neutral_low))
        self._nh = Decimal(str(neutral_high))
        self._atr_period = atr_period
        self._stop_mult = Decimal(str(atr_stop_mult))
        self._highs: list[Decimal] = []
        self._lows: list[Decimal] = []
        self._closes: list[Decimal] = []
        self._position: SignalAction = SignalAction.HOLD

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._position = SignalAction.HOLD

    def on_bar(self, bar: Bar) -> Signal | None:
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        need = max(self._period, self._atr_period) + 1
        if len(self._closes) < need:
            return None

        rsi_series = rsi(self._closes, self._period)
        atr_series = atr(self._highs, self._lows, self._closes, self._atr_period)
        r = rsi_series[-1]
        a = atr_series[-1]
        if r is None or a is None:
            return None

        if self._position == SignalAction.HOLD:
            if r <= self._os:
                self._position = SignalAction.ENTER_LONG
                return Signal(
                    action=SignalAction.ENTER_LONG,
                    stop_loss=bar.close - self._stop_mult * a,
                    reason=f"rsi={r:.2f} ≤ {self._os}",
                )
            if r >= self._ob:
                self._position = SignalAction.ENTER_SHORT
                return Signal(
                    action=SignalAction.ENTER_SHORT,
                    stop_loss=bar.close + self._stop_mult * a,
                    reason=f"rsi={r:.2f} ≥ {self._ob}",
                )
            return None

        if self._position == SignalAction.ENTER_LONG and r >= self._nh:
            self._position = SignalAction.HOLD
            return Signal(action=SignalAction.EXIT, reason=f"rsi={r:.2f} ≥ {self._nh}")
        if self._position == SignalAction.ENTER_SHORT and r <= self._nl:
            self._position = SignalAction.HOLD
            return Signal(action=SignalAction.EXIT, reason=f"rsi={r:.2f} ≤ {self._nl}")
        return None
