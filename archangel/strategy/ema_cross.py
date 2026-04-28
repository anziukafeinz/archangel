"""EMA-cross trend-following strategy.

Enters **long** when the fast EMA crosses above the slow EMA, **short** when
it crosses below, and exits the opposite way. The stop-loss is offset from
the last close by ``atr_stop_mult × ATR(atr_period)``; an optional
take-profit uses ``atr_target_mult × ATR`` in the opposite direction.
"""

from __future__ import annotations

from decimal import Decimal

from archangel.strategy.base import Bar, Signal, SignalAction, Strategy
from archangel.strategy.indicators import atr, ema


class EmaCrossStrategy(Strategy):
    """Classic two-EMA crossover with ATR-scaled stops.

    Parameters are tunable via the constructor so the same class backs a
    family of strategies (e.g. 9/21 scalper vs 50/200 swing).
    """

    name = "ema_cross"

    def __init__(
        self,
        *,
        fast: int = 12,
        slow: int = 26,
        atr_period: int = 14,
        atr_stop_mult: Decimal = Decimal("2"),
        atr_target_mult: Decimal | None = Decimal("3"),
    ) -> None:
        if fast >= slow:
            raise ValueError("fast period must be < slow period")
        self._fast = fast
        self._slow = slow
        self._atr_period = atr_period
        self._stop_mult = Decimal(str(atr_stop_mult))
        self._target_mult = None if atr_target_mult is None else Decimal(str(atr_target_mult))
        self._highs: list[Decimal] = []
        self._lows: list[Decimal] = []
        self._closes: list[Decimal] = []
        self._last_state: SignalAction = SignalAction.HOLD  # current desired position

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._last_state = SignalAction.HOLD

    def on_bar(self, bar: Bar) -> Signal | None:
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)

        if len(self._closes) < max(self._slow, self._atr_period) + 1:
            return None

        fast_series = ema(self._closes, self._fast)
        slow_series = ema(self._closes, self._slow)
        atr_series = atr(self._highs, self._lows, self._closes, self._atr_period)

        f_now, f_prev = fast_series[-1], fast_series[-2]
        s_now, s_prev = slow_series[-1], slow_series[-2]
        a = atr_series[-1]
        if None in (f_now, f_prev, s_now, s_prev, a):
            return None
        assert f_now is not None and f_prev is not None
        assert s_now is not None and s_prev is not None
        assert a is not None

        crossed_up = f_prev <= s_prev and f_now > s_now
        crossed_down = f_prev >= s_prev and f_now < s_now

        if crossed_up and self._last_state != SignalAction.ENTER_LONG:
            self._last_state = SignalAction.ENTER_LONG
            stop = bar.close - self._stop_mult * a
            tp = None if self._target_mult is None else bar.close + self._target_mult * a
            return Signal(
                action=SignalAction.ENTER_LONG,
                stop_loss=stop,
                take_profit=tp,
                reason=f"ema{self._fast}>ema{self._slow} (atr={a:.4f})",
            )
        if crossed_down and self._last_state != SignalAction.ENTER_SHORT:
            self._last_state = SignalAction.ENTER_SHORT
            stop = bar.close + self._stop_mult * a
            tp = None if self._target_mult is None else bar.close - self._target_mult * a
            return Signal(
                action=SignalAction.ENTER_SHORT,
                stop_loss=stop,
                take_profit=tp,
                reason=f"ema{self._fast}<ema{self._slow} (atr={a:.4f})",
            )
        return None
