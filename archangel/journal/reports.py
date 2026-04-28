"""Aggregations over the local journal store."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from archangel.journal.store import IncomeRow, JournalStore


@dataclass(frozen=True)
class DailyPnL:
    day: date
    realized: Decimal
    funding: Decimal
    commission: Decimal

    @property
    def net(self) -> Decimal:
        # Binance returns commission as a negative number already. We sum
        # additively and treat the sign as authoritative.
        return self.realized + self.funding + self.commission


@dataclass(frozen=True)
class SymbolSummary:
    symbol: str
    realized: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    rows_by_type: dict[str, Decimal] = field(default_factory=dict)

    @property
    def net(self) -> Decimal:
        return self.realized + self.funding + self.commission

    @property
    def win_rate(self) -> Decimal:
        decided = self.win_count + self.loss_count
        if decided == 0:
            return Decimal("0")
        return (Decimal(self.win_count) / Decimal(decided) * Decimal("100")).quantize(
            Decimal("0.01")
        )


def _date_for(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date()


def daily_pnl(rows: list[IncomeRow]) -> list[DailyPnL]:
    """Aggregate rows by UTC date. Days with no activity are omitted."""
    bucket: dict[date, dict[str, Decimal]] = defaultdict(
        lambda: {
            "REALIZED_PNL": Decimal("0"),
            "FUNDING_FEE": Decimal("0"),
            "COMMISSION": Decimal("0"),
        }
    )
    for row in rows:
        if row.income_type not in bucket[_date_for(row.time_ms)]:
            continue
        bucket[_date_for(row.time_ms)][row.income_type] += row.income

    return [
        DailyPnL(
            day=day,
            realized=values["REALIZED_PNL"],
            funding=values["FUNDING_FEE"],
            commission=values["COMMISSION"],
        )
        for day, values in sorted(bucket.items())
    ]


def _summary(rows: list[IncomeRow], *, label: str) -> SymbolSummary:
    realized = funding = commission = Decimal("0")
    by_type: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    wins = losses = 0
    realized_trades = [r for r in rows if r.income_type == "REALIZED_PNL"]

    for row in rows:
        by_type[row.income_type] += row.income
        if row.income_type == "REALIZED_PNL":
            realized += row.income
        elif row.income_type == "FUNDING_FEE":
            funding += row.income
        elif row.income_type == "COMMISSION":
            commission += row.income

    for trade in realized_trades:
        if trade.income > 0:
            wins += 1
        elif trade.income < 0:
            losses += 1

    return SymbolSummary(
        symbol=label,
        realized=realized,
        funding=funding,
        commission=commission,
        trade_count=len(realized_trades),
        win_count=wins,
        loss_count=losses,
        rows_by_type=dict(by_type),
    )


def summary_for_symbol(store: JournalStore, symbol: str) -> SymbolSummary:
    rows = store.all_rows(symbol=symbol)
    return _summary(rows, label=symbol.upper())


def total_summary(store: JournalStore) -> SymbolSummary:
    rows = store.all_rows()
    return _summary(rows, label="TOTAL")
