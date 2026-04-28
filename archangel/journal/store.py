"""SQLite-backed store for Binance income rows.

Money is stored as ``TEXT`` (Decimal stringified) to avoid float drift —
SQLite has no native Decimal type. All in-memory arithmetic uses
:class:`decimal.Decimal`. Aggregations happen Python-side, never in SQL.

The ``incomes`` table mirrors the Binance ``/fapi/v1/income`` payload
1-to-1 with one important addition: a unique key on ``(tran_id, symbol,
income_type, time_ms)`` so :meth:`upsert_many` is idempotent across reruns
of ``archangel journal sync``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS incomes (
    tran_id INTEGER NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    income_type TEXT NOT NULL,
    income TEXT NOT NULL,
    asset TEXT NOT NULL,
    info TEXT NOT NULL DEFAULT '',
    time_ms INTEGER NOT NULL,
    trade_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tran_id, symbol, income_type, time_ms)
);
CREATE INDEX IF NOT EXISTS idx_incomes_time ON incomes (time_ms);
CREATE INDEX IF NOT EXISTS idx_incomes_symbol ON incomes (symbol);
CREATE INDEX IF NOT EXISTS idx_incomes_type ON incomes (income_type);
"""


@dataclass(frozen=True)
class IncomeRow:
    """One row from Binance ``/fapi/v1/income``."""

    tran_id: int
    symbol: str
    income_type: str
    income: Decimal
    asset: str
    info: str
    time_ms: int
    trade_id: str

    @classmethod
    def from_binance(cls, payload: dict) -> IncomeRow:
        return cls(
            tran_id=int(payload.get("tranId", 0)),
            symbol=str(payload.get("symbol", "")),
            income_type=str(payload.get("incomeType", "")),
            income=Decimal(str(payload.get("income", "0"))),
            asset=str(payload.get("asset", "")),
            info=str(payload.get("info", "")),
            time_ms=int(payload.get("time", 0)),
            trade_id=str(payload.get("tradeId", "")),
        )


class JournalStore:
    """Tiny DAL over a SQLite file."""

    def __init__(self, path: str | Path = "~/.archangel/journal.db") -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_many(self, rows: list[IncomeRow]) -> int:
        """Insert rows; existing primary keys are skipped. Returns # inserted."""
        if not rows:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                """
                INSERT OR IGNORE INTO incomes
                    (tran_id, symbol, income_type, income, asset, info, time_ms, trade_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r.tran_id,
                        r.symbol,
                        r.income_type,
                        str(r.income),
                        r.asset,
                        r.info,
                        r.time_ms,
                        r.trade_id,
                    )
                    for r in rows
                ],
            )
            return cur.rowcount or 0

    def latest_time_ms(self) -> int | None:
        """Largest ``time_ms`` currently stored, or ``None`` if empty."""
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(time_ms) AS t FROM incomes").fetchone()
            value = row["t"] if row is not None else None
            return int(value) if value is not None else None

    def all_rows(
        self,
        *,
        symbol: str | None = None,
        income_type: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[IncomeRow]:
        """Filtered fetch. Empty filters return everything, oldest first."""
        clauses: list[str] = []
        params: list[object] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if income_type:
            clauses.append("income_type = ?")
            params.append(income_type.upper())
        if start_ms is not None:
            clauses.append("time_ms >= ?")
            params.append(start_ms)
        if end_ms is not None:
            clauses.append("time_ms <= ?")
            params.append(end_ms)
        sql = "SELECT * FROM incomes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY time_ms ASC"
        with self._connect() as conn:
            return [
                IncomeRow(
                    tran_id=int(row["tran_id"]),
                    symbol=str(row["symbol"]),
                    income_type=str(row["income_type"]),
                    income=Decimal(str(row["income"])),
                    asset=str(row["asset"]),
                    info=str(row["info"]),
                    time_ms=int(row["time_ms"]),
                    trade_id=str(row["trade_id"]),
                )
                for row in conn.execute(sql, params).fetchall()
            ]

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM incomes").fetchone()
            return int(row["n"]) if row is not None else 0
