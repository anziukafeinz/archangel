"""Unit tests for the trade journal: store, sync, and reports."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from archangel.journal.reports import (
    daily_pnl,
    summary_for_symbol,
    total_summary,
)
from archangel.journal.store import IncomeRow, JournalStore
from archangel.journal.sync import sync_incomes


def _ms(*, year: int = 2024, month: int = 1, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


# ---- store -------------------------------------------------------------


def test_store_creates_schema(tmp_path: Path) -> None:
    db = tmp_path / "j.db"
    store = JournalStore(db)
    assert db.exists()
    assert store.count() == 0


def test_upsert_is_idempotent(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    rows = [
        IncomeRow(
            tran_id=1,
            symbol="BTCUSDT",
            income_type="REALIZED_PNL",
            income=Decimal("12.5"),
            asset="USDT",
            info="",
            time_ms=_ms(day=2),
            trade_id="t1",
        ),
        IncomeRow(
            tran_id=2,
            symbol="BTCUSDT",
            income_type="COMMISSION",
            income=Decimal("-0.31"),
            asset="USDT",
            info="",
            time_ms=_ms(day=2),
            trade_id="t1",
        ),
    ]
    assert store.upsert_many(rows) == 2
    assert store.upsert_many(rows) == 0  # second insert is a no-op
    assert store.count() == 2


def test_latest_time_ms_tracks_max(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    assert store.latest_time_ms() is None
    store.upsert_many(
        [
            IncomeRow(
                tran_id=1,
                symbol="BTCUSDT",
                income_type="REALIZED_PNL",
                income=Decimal("1"),
                asset="USDT",
                info="",
                time_ms=_ms(day=1),
                trade_id="",
            ),
            IncomeRow(
                tran_id=2,
                symbol="BTCUSDT",
                income_type="REALIZED_PNL",
                income=Decimal("2"),
                asset="USDT",
                info="",
                time_ms=_ms(day=3),
                trade_id="",
            ),
        ]
    )
    assert store.latest_time_ms() == _ms(day=3)


def test_filtered_fetch(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    store.upsert_many(
        [
            IncomeRow(
                tran_id=1,
                symbol="BTCUSDT",
                income_type="REALIZED_PNL",
                income=Decimal("5"),
                asset="USDT",
                info="",
                time_ms=_ms(day=1),
                trade_id="",
            ),
            IncomeRow(
                tran_id=2,
                symbol="ETHUSDT",
                income_type="FUNDING_FEE",
                income=Decimal("-0.1"),
                asset="USDT",
                info="",
                time_ms=_ms(day=2),
                trade_id="",
            ),
        ]
    )
    btc = store.all_rows(symbol="BTCUSDT")
    assert len(btc) == 1 and btc[0].income == Decimal("5")
    funding = store.all_rows(income_type="FUNDING_FEE")
    assert len(funding) == 1 and funding[0].symbol == "ETHUSDT"


# ---- reports -----------------------------------------------------------


def _seed(store: JournalStore) -> None:
    store.upsert_many(
        [
            IncomeRow(
                tran_id=1,
                symbol="BTCUSDT",
                income_type="REALIZED_PNL",
                income=Decimal("10"),
                asset="USDT",
                info="",
                time_ms=_ms(day=1),
                trade_id="t1",
            ),
            IncomeRow(
                tran_id=2,
                symbol="BTCUSDT",
                income_type="COMMISSION",
                income=Decimal("-0.5"),
                asset="USDT",
                info="",
                time_ms=_ms(day=1),
                trade_id="t1",
            ),
            IncomeRow(
                tran_id=3,
                symbol="BTCUSDT",
                income_type="FUNDING_FEE",
                income=Decimal("-0.1"),
                asset="USDT",
                info="",
                time_ms=_ms(day=1),
                trade_id="",
            ),
            IncomeRow(
                tran_id=4,
                symbol="BTCUSDT",
                income_type="REALIZED_PNL",
                income=Decimal("-5"),
                asset="USDT",
                info="",
                time_ms=_ms(day=2),
                trade_id="t2",
            ),
            IncomeRow(
                tran_id=5,
                symbol="ETHUSDT",
                income_type="REALIZED_PNL",
                income=Decimal("3"),
                asset="USDT",
                info="",
                time_ms=_ms(day=2),
                trade_id="t3",
            ),
        ]
    )


def test_daily_pnl_aggregates_per_day(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    _seed(store)
    days = daily_pnl(store.all_rows())
    assert [d.day.isoformat() for d in days] == ["2024-01-01", "2024-01-02"]
    d1, d2 = days
    assert d1.realized == Decimal("10")
    assert d1.commission == Decimal("-0.5")
    assert d1.funding == Decimal("-0.1")
    assert d1.net == Decimal("9.4")
    assert d2.realized == Decimal("-2")  # -5 BTC + 3 ETH
    assert d2.net == Decimal("-2")


def test_summary_total(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    _seed(store)
    summary = total_summary(store)
    assert summary.symbol == "TOTAL"
    assert summary.realized == Decimal("8")  # 10 - 5 + 3
    assert summary.commission == Decimal("-0.5")
    assert summary.funding == Decimal("-0.1")
    assert summary.trade_count == 3
    assert summary.win_count == 2  # +10, +3
    assert summary.loss_count == 1  # -5
    assert summary.win_rate == Decimal("66.67")


def test_summary_per_symbol(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    _seed(store)
    btc = summary_for_symbol(store, "btcusdt")
    assert btc.symbol == "BTCUSDT"
    assert btc.realized == Decimal("5")  # 10 - 5
    assert btc.win_count == 1
    assert btc.loss_count == 1
    assert btc.win_rate == Decimal("50.00")


# ---- sync --------------------------------------------------------------


class _FakeIncomeFetcher:
    """Fake fetcher that paginates a fixed list by startTime."""

    def __init__(self, payloads: list[dict], *, page_size: int = 1000) -> None:
        self._payloads = sorted(payloads, key=lambda p: p["time"])
        self._page_size = page_size
        self.calls: list[dict] = []

    async def get_income_history(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        income_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        self.calls.append({"start_ms": start_ms, "end_ms": end_ms, "limit": limit})
        page_size = min(limit, self._page_size)
        rows = [
            p
            for p in self._payloads
            if (start_ms is None or p["time"] >= start_ms)
            and (end_ms is None or p["time"] <= end_ms)
        ]
        return rows[:page_size]


@pytest.mark.asyncio
async def test_sync_inserts_new_rows(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    fetcher = _FakeIncomeFetcher(
        [
            {
                "tranId": 1,
                "symbol": "BTCUSDT",
                "incomeType": "REALIZED_PNL",
                "income": "12.5",
                "asset": "USDT",
                "info": "",
                "time": _ms(day=1),
                "tradeId": "t1",
            },
            {
                "tranId": 2,
                "symbol": "BTCUSDT",
                "incomeType": "COMMISSION",
                "income": "-0.31",
                "asset": "USDT",
                "info": "",
                "time": _ms(day=2),
                "tradeId": "t1",
            },
        ]
    )
    inserted = await sync_incomes(fetcher, store)
    assert inserted == 2
    assert store.count() == 2


@pytest.mark.asyncio
async def test_sync_is_incremental(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    fetcher = _FakeIncomeFetcher(
        [
            {
                "tranId": 1,
                "symbol": "BTCUSDT",
                "incomeType": "REALIZED_PNL",
                "income": "10",
                "asset": "USDT",
                "info": "",
                "time": _ms(day=1),
                "tradeId": "t1",
            },
        ]
    )
    await sync_incomes(fetcher, store)
    # Add a newer row to the fetcher
    fetcher._payloads.append(
        {
            "tranId": 2,
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "5",
            "asset": "USDT",
            "info": "",
            "time": _ms(day=5),
            "tradeId": "t2",
        }
    )
    inserted = await sync_incomes(fetcher, store)
    assert inserted == 1
    assert store.count() == 2


@pytest.mark.asyncio
async def test_sync_paginates_when_full_page(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "j.db")
    payloads = [
        {
            "tranId": i,
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "1",
            "asset": "USDT",
            "info": "",
            "time": _ms(day=1) + i * 1000,
            "tradeId": f"t{i}",
        }
        for i in range(1, 6)
    ]
    fetcher = _FakeIncomeFetcher(payloads, page_size=2)
    inserted = await sync_incomes(fetcher, store, page_limit=2)
    assert inserted == 5
    # 5 rows, page=2 → at least 3 fetcher calls (2+2+1)
    assert len(fetcher.calls) >= 3
