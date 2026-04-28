"""Position sizing helpers.

The core idea: given account equity ``E``, a per-trade risk budget ``r`` (as
percent of equity), an entry price ``Pe`` and a stop-loss price ``Ps``, the
quantity that loses exactly ``E * r/100`` if stopped out is::

    qty = (E * r / 100) / |Pe - Ps|

This is what every risk-disciplined trader does manually with a calculator;
having it in code lets us reject any order that would breach the budget.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def position_size_from_risk(
    *,
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_loss_price: Decimal,
    qty_step: Decimal = Decimal("0"),
    min_qty: Decimal = Decimal("0"),
) -> Decimal:
    """Return the quantity such that ``|entry - stop| * qty == equity * risk_pct/100``.

    The result is rounded *down* to ``qty_step`` so we never exceed the budget.
    Returns ``Decimal("0")`` if the rounded size is below ``min_qty``.

    Raises:
        ValueError: if any input is non-positive or entry == stop.
    """
    if equity <= 0:
        raise ValueError("equity must be positive")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be positive")
    if entry_price <= 0 or stop_loss_price <= 0:
        raise ValueError("prices must be positive")
    if entry_price == stop_loss_price:
        raise ValueError("entry_price and stop_loss_price must differ")

    risk_budget = equity * (risk_pct / Decimal("100"))
    distance = abs(entry_price - stop_loss_price)
    raw_qty = risk_budget / distance

    if qty_step > 0:
        raw_qty = (raw_qty / qty_step).quantize(Decimal("1"), rounding=ROUND_DOWN) * qty_step

    if min_qty > 0 and raw_qty < min_qty:
        return Decimal("0")
    return raw_qty
