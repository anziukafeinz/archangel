"""Performance metrics for a backtest run.

Kept deliberately small and dependency-free (no numpy). Calculations use
``Decimal`` throughout except where ratios would explode precision (Sharpe
/ Sortino are returned as floats).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archangel.backtest.engine import Trade


@dataclass(frozen=True)
class PerformanceMetrics:
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    profit_factor: Decimal  # total wins / |total losses|, 0 when no losses
    expectancy: Decimal  # avg PnL per trade
    sharpe: float  # annualised with sqrt(bars_per_year) assuming 1 bar = 1d
    sortino: float


def compute_metrics(
    *,
    initial_equity: Decimal,
    final_equity: Decimal,
    trades: Sequence[Trade],
    equity_curve: Sequence[tuple[int, Decimal]],
    bars_per_year: int = 365,
) -> PerformanceMetrics:
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum((t.pnl for t in wins), Decimal("0"))
    gross_loss = sum((t.pnl for t in losses), Decimal("0"))  # negative
    pf = Decimal("0")
    if gross_loss != 0:
        pf = (gross_win / -gross_loss).quantize(Decimal("0.0001"))
    elif gross_win > 0:
        pf = Decimal("999.9999")
    win_rate = (
        (Decimal(len(wins)) / Decimal(n) * Decimal("100")).quantize(Decimal("0.01"))
        if n > 0
        else Decimal("0")
    )
    total_return = (
        ((final_equity - initial_equity) / initial_equity * Decimal("100")).quantize(
            Decimal("0.0001")
        )
        if initial_equity != 0
        else Decimal("0")
    )
    mdd = _max_drawdown_pct(equity_curve)
    expectancy = (
        (sum((t.pnl for t in trades), Decimal("0")) / Decimal(n)).quantize(Decimal("0.0001"))
        if n > 0
        else Decimal("0")
    )
    sharpe = _sharpe(equity_curve, bars_per_year, downside_only=False)
    sortino = _sharpe(equity_curve, bars_per_year, downside_only=True)
    return PerformanceMetrics(
        trade_count=n,
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=win_rate,
        total_return_pct=total_return,
        max_drawdown_pct=mdd,
        profit_factor=pf,
        expectancy=expectancy,
        sharpe=sharpe,
        sortino=sortino,
    )


def _max_drawdown_pct(curve: Sequence[tuple[int, Decimal]]) -> Decimal:
    if not curve:
        return Decimal("0")
    peak = curve[0][1]
    max_dd = Decimal("0")
    for _, eq in curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * Decimal("100")
            if dd > max_dd:
                max_dd = dd
    return max_dd.quantize(Decimal("0.0001"))


def _sharpe(
    curve: Sequence[tuple[int, Decimal]],
    periods_per_year: int,
    *,
    downside_only: bool,
) -> float:
    if len(curve) < 2:
        return 0.0
    # Per-bar simple returns
    rets: list[float] = []
    for i in range(1, len(curve)):
        prev = curve[i - 1][1]
        cur = curve[i][1]
        if prev <= 0:
            continue
        rets.append(float((cur - prev) / prev))
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    if downside_only:
        negs = [r for r in rets if r < 0]
        if not negs:
            return 0.0
        var = sum((r - 0.0) ** 2 for r in negs) / len(negs)
    else:
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)
