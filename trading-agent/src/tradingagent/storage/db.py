"""Async database access, batched writes and fail-closed semantics.

* `Database.write_now(...)` — awaited, used for anything that must be durable BEFORE the next step (orders before
  broadcast, kill-switch state). Raises DatabaseUnavailable; callers fail closed.
* `BatchWriter` — buffered inserts for high-volume append-only data (events, snapshots, features, decisions).
  Persistent flush failures are reported to the kill switch (which stops entries after N errors).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from tradingagent.common.logging import get_logger, register_secret
from tradingagent.common.metrics import METRICS
from tradingagent.storage.models import Base, RuntimeState

log = get_logger("storage.db")


class DatabaseUnavailable(RuntimeError):
    pass


class Database:
    def __init__(self, url: str, echo: bool = False) -> None:
        register_secret(url)
        self.url = url
        kw: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
        if url.startswith("postgresql"):
            kw.update(pool_size=10, max_overflow=10)
        self.engine: AsyncEngine = create_async_engine(url, **kw)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)
        self.dialect = self.engine.dialect.name

    async def create_all(self) -> None:
        """Development/test only. Production uses Alembic migrations (which also install immutability triggers)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except (SQLAlchemyError, OSError):
            return False

    def _insert(self, table: Any, rows: list[dict], ignore_conflicts: bool):
        if ignore_conflicts and self.dialect == "postgresql":
            return pg_insert(table).values(rows).on_conflict_do_nothing()
        if ignore_conflicts and self.dialect == "sqlite":
            return sqlite_insert(table).values(rows).on_conflict_do_nothing()
        return insert(table).values(rows)

    async def insert_rows(self, table: Any, rows: list[dict], ignore_conflicts: bool = False) -> None:
        if not rows:
            return
        try:
            async with self.engine.begin() as conn:
                # chunk to stay below driver parameter limits
                cols = max(1, len(rows[0]))
                step = max(1, 30_000 // cols)
                for i in range(0, len(rows), step):
                    await conn.execute(self._insert(table, rows[i : i + step], ignore_conflicts))
        except (SQLAlchemyError, OSError) as e:
            raise DatabaseUnavailable(f"{type(e).__name__}: {str(e)[:300]}") from e

    async def upsert(self, table: Any, row: dict, key: str) -> None:
        try:
            async with self.engine.begin() as conn:
                if self.dialect == "postgresql":
                    stmt = pg_insert(table).values(row)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[key], set_={k: v for k, v in row.items() if k != key}
                    )
                elif self.dialect == "sqlite":
                    stmt = sqlite_insert(table).values(row)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[key], set_={k: v for k, v in row.items() if k != key}
                    )
                else:
                    raise DatabaseUnavailable(f"unsupported dialect {self.dialect}")
                await conn.execute(stmt)
        except (SQLAlchemyError, OSError) as e:
            raise DatabaseUnavailable(f"{type(e).__name__}: {str(e)[:300]}") from e

    async def upsert_many(self, table: Any, rows: list[dict], key: str) -> None:
        if not rows:
            return
        try:
            async with self.engine.begin() as conn:
                ins = pg_insert(table) if self.dialect == "postgresql" else sqlite_insert(table)
                cols = [c for c in rows[0] if c != key]
                step = max(1, 20_000 // max(1, len(rows[0])))
                for i in range(0, len(rows), step):
                    stmt = ins.values(rows[i : i + step])
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[key], set_={c: getattr(stmt.excluded, c) for c in cols}
                    )
                    await conn.execute(stmt)
        except (SQLAlchemyError, OSError) as e:
            raise DatabaseUnavailable(f"{type(e).__name__}: {str(e)[:300]}") from e

    async def write_now(self, table: Any, rows: list[dict] | dict, upsert_key: str | None = None) -> None:
        if isinstance(rows, dict):
            rows = [rows]
        if upsert_key:
            for r in rows:
                await self.upsert(table, r, upsert_key)
        else:
            await self.insert_rows(table, rows)

    async def set_state(self, key: str, value: dict) -> None:
        await self.upsert(RuntimeState.__table__, {"key": key, "value": value, "updated_at": time.time()}, "key")

    async def get_state(self, key: str) -> tuple[dict | None, float | None]:
        try:
            async with self.session() as s:
                row = (await s.execute(select(RuntimeState).where(RuntimeState.key == key))).scalar_one_or_none()
                return (row.value, row.updated_at) if row else (None, None)
        except (SQLAlchemyError, OSError) as e:
            raise DatabaseUnavailable(str(e)[:300]) from e


class BatchWriter:
    def __init__(
        self,
        db: Database,
        batch_size: int = 500,
        flush_interval_s: float = 1.0,
        max_queue: int = 200_000,
        on_error: Callable[[str], None] | None = None,
        on_ok: Callable[[], None] | None = None,
    ) -> None:
        self.db, self.batch_size, self.flush_interval_s, self.max_queue = db, batch_size, flush_interval_s, max_queue
        self.on_error, self.on_ok = on_error, on_ok
        self._queues: dict[Any, deque[dict]] = defaultdict(deque)
        self._ignore: dict[Any, bool] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.dropped = 0
        self.consecutive_failures = 0

    def put(self, table: Any, row: dict, ignore_conflicts: bool = False) -> None:
        q = self._queues[table]
        if sum(len(x) for x in self._queues.values()) >= self.max_queue:
            self.dropped += 1
            METRICS.inc("storage.dropped_rows")
            return
        q.append(row)
        self._ignore[table] = ignore_conflicts or self._ignore.get(table, False)

    @property
    def backlog(self) -> int:
        return sum(len(q) for q in self._queues.values())

    async def flush(self) -> None:
        for table, q in list(self._queues.items()):
            while q:
                batch = [q.popleft() for _ in range(min(self.batch_size, len(q)))]
                try:
                    await self.db.insert_rows(table, batch, self._ignore.get(table, False))
                    METRICS.inc("storage.rows_written", len(batch))
                    self.consecutive_failures = 0
                    if self.on_ok:
                        self.on_ok()
                except DatabaseUnavailable as e:
                    for r in reversed(batch):
                        q.appendleft(r)
                    self.consecutive_failures += 1
                    METRICS.inc("storage.flush_errors")
                    log.error("db_flush_failed", table=str(getattr(table, "name", table)), error=str(e)[:200])
                    if self.on_error:
                        self.on_error(str(e)[:200])
                    return

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.flush_interval_s)
            except TimeoutError:
                pass
            await self.flush()
            METRICS.set("storage.backlog", self.backlog)
            if self.consecutive_failures:
                await asyncio.sleep(min(30.0, 0.5 * 2 ** min(self.consecutive_failures, 6)))

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="batch-writer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        await self.flush()
