"""WebSocket `logsSubscribe` client with reconnect, staleness watchdog and endpoint rotation.

Gaps are explicit: every reconnect emits a `gap` callback with the last slot seen before the drop, so the system
can mark data as incomplete (and optionally backfill via getSignaturesForAddress) instead of silently trading on
a hole in its event history.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import websockets

from tradingagent.common.logging import get_logger, register_secret
from tradingagent.common.metrics import METRICS

log = get_logger("solana.ws")


class LogsSubscriber:
    def __init__(
        self,
        urls: list[str],
        programs: list[str],
        commitment: str = "confirmed",
        stale_after_s: float = 20.0,
        ping_interval_s: float = 10.0,
        max_backoff_s: float = 15.0,
        on_gap: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        if not urls:
            raise ValueError("LogsSubscriber needs at least one websocket URL")
        for u in urls:
            register_secret(u)
        self.urls, self.programs, self.commitment = urls, programs, commitment
        self.stale_after_s, self.ping_interval_s, self.max_backoff_s = stale_after_s, ping_interval_s, max_backoff_s
        self.on_gap = on_gap
        self.last_message_at = 0.0
        self.last_slot = 0
        self.connected = False
        self.reconnects = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    @property
    def staleness_s(self) -> float:
        return time.time() - self.last_message_at if self.last_message_at else float("inf")

    async def messages(self) -> AsyncIterator[tuple[dict, float]]:
        """Yield (logsNotification message, observed_at). Runs until stop()."""
        backoff = 0.25
        idx = 0
        while not self._stop.is_set():
            url = self.urls[idx % len(self.urls)]
            label = f"ws{idx % len(self.urls)}"
            try:
                async with websockets.connect(
                    url,
                    ping_interval=self.ping_interval_s,
                    ping_timeout=self.ping_interval_s,
                    max_size=2**22,
                    open_timeout=10,
                ) as ws:
                    for i, prog in enumerate(self.programs):
                        await ws.send(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": i + 1,
                                    "method": "logsSubscribe",
                                    "params": [{"mentions": [prog]}, {"commitment": self.commitment}],
                                }
                            )
                        )
                    self.connected = True
                    if self.reconnects and self.on_gap:
                        await self.on_gap(self.last_slot, "ws_reconnect")
                    log.info("ws_connected", endpoint=label, programs=len(self.programs))
                    METRICS.set("ws.connected", 1)
                    backoff = 0.25
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after_s)
                        except TimeoutError:
                            log.warning("ws_stale_reconnect", endpoint=label, stale_after_s=self.stale_after_s)
                            METRICS.inc("ws.stale")
                            break
                        now = time.time()
                        self.last_message_at = now
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            METRICS.inc("ws.bad_json")
                            continue
                        if msg.get("method") != "logsNotification":
                            continue
                        slot = ((msg.get("params") or {}).get("result") or {}).get("context", {}).get("slot")
                        if slot:
                            self.last_slot = max(self.last_slot, int(slot))
                        METRICS.inc("ws.messages")
                        yield msg, now
            except (OSError, websockets.WebSocketException, TimeoutError) as e:
                log.warning("ws_error", endpoint=label, error=f"{type(e).__name__}: {e}")
                METRICS.inc("ws.errors")
            self.connected = False
            METRICS.set("ws.connected", 0)
            if self._stop.is_set():
                break
            self.reconnects += 1
            idx += 1  # rotate provider
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff_s)
