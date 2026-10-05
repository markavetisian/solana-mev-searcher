"""Event sources. Every source yields MarketEvents in non-decreasing (observed_at, slot, seq) order."""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Protocol

from tradingagent.common.events import MarketEvent
from tradingagent.common.logging import get_logger
from tradingagent.common.metrics import METRICS
from tradingagent.common.types import EventKind
from tradingagent.ingestion.parser import LogParser, parse_logs_notification
from tradingagent.pump.constants import PUMP_AMM_PROGRAM_ID, PUMP_PROGRAM_ID
from tradingagent.solana.ws import LogsSubscriber

log = get_logger("ingestion.sources")


class EventSource(Protocol):
    def events(self) -> AsyncIterator[MarketEvent]: ...


class WebSocketLogSource:
    """Live on-chain events via logsSubscribe(mentions=[pump, pump_amm])."""

    def __init__(
        self,
        ws_urls: list[str],
        parser: LogParser,
        commitment: str = "confirmed",
        include_amm: bool = True,
        stale_after_s: float = 20.0,
        ping_interval_s: float = 10.0,
        max_backoff_s: float = 15.0,
    ) -> None:
        programs = [str(PUMP_PROGRAM_ID)] + ([str(PUMP_AMM_PROGRAM_ID)] if include_amm else [])
        self._gaps: asyncio.Queue[MarketEvent] = asyncio.Queue()
        self.sub = LogsSubscriber(
            ws_urls, programs, commitment, stale_after_s, ping_interval_s, max_backoff_s, on_gap=self._on_gap
        )
        self.parser = parser
        self._seen: dict[str, None] = {}  # signature dedup across both subscriptions (bounded)

    async def _on_gap(self, last_slot: int, reason: str) -> None:
        import time

        self._gaps.put_nowait(
            MarketEvent(
                kind=EventKind.DATA_GAP,
                signature=f"gap-{last_slot}-{time.time():.3f}",
                slot=last_slot,
                seq=0,
                observed_at=time.time(),
                chain_time=0.0,
                extra={"reason": reason},
            )
        )

    @property
    def staleness_s(self) -> float:
        return self.sub.staleness_s

    @property
    def connected(self) -> bool:
        return self.sub.connected

    def stop(self) -> None:
        self.sub.stop()

    async def events(self) -> AsyncIterator[MarketEvent]:
        async for msg, observed_at in self.sub.messages():
            while not self._gaps.empty():
                yield self._gaps.get_nowait()
            res = parse_logs_notification(self.parser, msg, observed_at)
            if res is None or not res.events:
                continue
            sig = res.events[0].signature
            if sig in self._seen:  # tx mentioning both programs arrives twice
                METRICS.inc("ingest.duplicate_tx")
                continue
            self._seen[sig] = None
            if len(self._seen) > 50_000:
                for k in list(self._seen)[:25_000]:
                    del self._seen[k]
            for ev in res.events:
                METRICS.inc(f"ingest.events.{ev.kind.value}")
                yield ev


def _open(path: Path):
    return gzip.open(path, "rt") if path.suffix == ".gz" else path.open("r")


def iter_jsonl(path: str | Path) -> Iterator[MarketEvent]:
    p = Path(path)
    files = sorted(p.glob("*.jsonl*")) if p.is_dir() else [p]
    for f in files:
        with _open(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield MarketEvent.from_dict(json.loads(line))


class JsonlReplaySource:
    """Replay recorded events from JSONL file(s). Order is verified, not assumed."""

    def __init__(self, path: str | Path) -> None:
        self.path = path

    async def events(self) -> AsyncIterator[MarketEvent]:
        last = None
        for ev in iter_jsonl(self.path):
            if last is not None and ev.observed_at < last:
                raise ValueError(f"JSONL not time-ordered at {ev.event_id}: {ev.observed_at} < {last}")
            last = ev.observed_at
            yield ev


class ListSource:
    def __init__(self, events: list[MarketEvent]) -> None:
        self._events = sorted(events, key=lambda e: e.order_key)

    async def events(self) -> AsyncIterator[MarketEvent]:
        for ev in self._events:
            yield ev


class JsonlArchiver:
    """Append-only raw event archive (one file per UTC day). Cheap insurance for research reproducibility."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._day = None

    def write(self, ev: MarketEvent) -> None:
        import time

        day = time.strftime("%Y%m%d", time.gmtime(ev.observed_at))
        if day != self._day:
            if self._fh:
                self._fh.close()
            self._fh = (self.dir / f"events-{day}.jsonl").open("a")
            self._day = day
        self._fh.write(json.dumps(ev.to_dict(), separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self._fh:
            self._fh.close()
