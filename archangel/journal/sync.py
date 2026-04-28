"""Pull Binance income history into the local journal store."""

from __future__ import annotations

import logging
import time
from typing import Protocol

from archangel.journal.store import IncomeRow, JournalStore

logger = logging.getLogger(__name__)


class _IncomeFetcher(Protocol):
    async def get_income_history(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        income_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict]: ...


async def sync_incomes(
    client: _IncomeFetcher,
    store: JournalStore,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    page_limit: int = 1000,
) -> int:
    """Sync income rows into ``store`` and return the number newly inserted.

    If ``start_ms`` is omitted, resumes from the latest ``time_ms`` in the
    store +1ms (so a re-sync is always incremental). Pagination cursors on
    ``startTime`` rather than the page index because Binance's ``/income``
    endpoint caps at 1000 rows per call and only returns the most recent
    1000 within a 7-day window.
    """
    if start_ms is None:
        latest = store.latest_time_ms()
        start_ms = (latest + 1) if latest is not None else 0
    if end_ms is None:
        end_ms = int(time.time() * 1000)

    inserted_total = 0
    cursor = start_ms

    while True:
        payload = await client.get_income_history(start_ms=cursor, end_ms=end_ms, limit=page_limit)
        if not payload:
            break
        rows = [IncomeRow.from_binance(p) for p in payload]
        inserted = store.upsert_many(rows)
        inserted_total += inserted
        # Advance cursor past the newest row in this batch
        max_time = max(r.time_ms for r in rows)
        if max_time <= cursor:
            # Safety: avoid infinite loops if a page returns nothing newer
            break
        cursor = max_time + 1
        if len(payload) < page_limit:
            # Less than a full page — no more rows in this window
            break
        logger.info("Sync progress: %d rows inserted, cursor=%d", inserted_total, cursor)

    return inserted_total
