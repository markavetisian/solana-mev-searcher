"""Load recorded market events from PostgreSQL/SQLite for backtests and research (ordered, paginated)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import select

from tradingagent.common.events import MarketEvent
from tradingagent.storage import models as m
from tradingagent.storage.db import Database
from tradingagent.storage.sink import row_to_event


async def iter_db_events(
    db: Database,
    start: float | None = None,
    end: float | None = None,
    page: int = 20_000,
    include_synthetic: bool = False,
) -> AsyncIterator[MarketEvent]:
    last_id = 0
    while True:
        q = select(m.MarketEventRow).where(m.MarketEventRow.id > last_id).order_by(m.MarketEventRow.id).limit(page)
        if start is not None:
            q = q.where(m.MarketEventRow.observed_at >= start)
        if end is not None:
            q = q.where(m.MarketEventRow.observed_at < end)
        if not include_synthetic:
            q = q.where(m.MarketEventRow.source != "synthetic")
        async with db.session() as s:
            rows = (await s.execute(q)).scalars().all()
        if not rows:
            return
        for r in rows:
            yield row_to_event(r)
        last_id = rows[-1].id


async def load_db_events(
    db: Database, start: float | None = None, end: float | None = None, include_synthetic: bool = False
) -> list[MarketEvent]:
    out = [e async for e in iter_db_events(db, start, end, include_synthetic=include_synthetic)]
    out.sort(key=lambda e: e.order_key)  # insertion order ~ arrival order; enforce the replay key explicitly
    return out
