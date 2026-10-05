"""Operator alerts (Telegram). Plain text only (no parse_mode), so token metadata cannot inject formatting or
links; untrusted strings are sanitized and truncated. Messages are queued, rate-limited and never block trading."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Protocol

import httpx

from tradingagent.common.logging import get_logger, register_secret
from tradingagent.common.metrics import METRICS
from tradingagent.common.sanitize import clean_untrusted
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.portfolio.portfolio import ClosedTrade, Position
from tradingagent.strategy.decision import Decision

log = get_logger("alerts")


class AlertSink(Protocol):
    def send(self, text: str, severity: str = "INFO") -> None: ...


def _sym(symbol: str | None, mint: str) -> str:
    s = clean_untrusted(symbol, 16)
    return f"{s} ({mint[:4]}…{mint[-4:]})" if s else mint


def fmt_opportunity(d: Decision, mode: str) -> str:
    f = d.features
    ev = d.ev
    lines = [
        f"[{mode}] NEW HIGH-SCORE OPPORTUNITY",
        "",
        _sym(d.symbol, d.mint),
        "",
        f"Score: {d.score.total:.0f}" if d.score else "Score: n/a",
        f"Decision: {d.outcome.value}",
        f"Regime: {d.regime.regime.value}" if d.regime else "",
    ]
    if ev is not None:
        lines.append(
            f"Expected EV: {'INSUFFICIENT DATA' if ev.status != 'OK' else f'{ev.ev_conservative:+.2%} (conservative)'}"
        )
    if f is not None:
        lines += [
            f"Liquidity: {f.get('liquidity_sol', 0) or 0:.2f} SOL",
            f"Volume accel (30s): {f.get('vol_accel_30s', 0) or 0:.2f}x",
            f"Holder growth (60s): {f.get('holder_growth_60s', 0) or 0:+.1%}",
        ]
    if d.sizing and d.sizing.size_sol > 0:
        lines.append(f"Proposed size: {d.sizing.size_sol:.4f} SOL")
    if d.execution:
        lines.append(f"Entry: {d.execution.expected_price:.3e} lamports/unit (impact {d.execution.price_impact:.2%})")
    if d.rejection_reasons:
        lines.append("Blocked by: " + "; ".join(d.rejection_reasons[:3]))
    return "\n".join(x for x in lines if x is not None)


def fmt_opened(pos: Position, mode: str) -> str:
    return "\n".join(
        [
            f"[{mode}] POSITION OPENED",
            "",
            _sym(pos.symbol, pos.mint),
            f"Venue: {pos.venue.value}",
            f"Entry: {pos.entry_effective_price:.3e} lamports/unit",
            f"Size: {pos.initial_cost_lamports / LAMPORTS_PER_SOL:.4f} SOL",
            f"Expected slippage: {pos.expected_entry_slippage:.2%}",
            f"Actual slippage: {pos.actual_entry_slippage:.2%}",
            f"Invalidation: -{pos.hard_stop_frac:.0%} executable value",
        ]
    )


def fmt_closed(ct: ClosedTrade, mode: str) -> str:
    return "\n".join(
        [
            f"[{mode}] POSITION CLOSED",
            "",
            _sym(ct.symbol, ct.mint),
            f"Cost: {ct.cost_lamports / LAMPORTS_PER_SOL:.4f} SOL",
            f"Proceeds: {ct.proceeds_lamports / LAMPORTS_PER_SOL:.4f} SOL",
            f"P/L: {ct.pnl_lamports / LAMPORTS_PER_SOL:+.4f} SOL ({ct.net_return:+.1%})",
            f"Held: {ct.hold_s:.0f}s",
            f"Reason: {ct.exit_reason}",
        ]
    )


def fmt_halt(reason: str, mode: str) -> str:
    return f"[{mode}] TRADING HALTED\n\nReason:\n{clean_untrusted(reason, 400)}"


class LogAlertSink:
    def send(self, text: str, severity: str = "INFO") -> None:
        log.info("alert", severity=severity, text=text)


class TelegramAlertSink:
    def __init__(self, bot_token: str, chat_id: str, max_per_minute: int = 20) -> None:
        register_secret(bot_token)
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.max_per_minute = max_per_minute
        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._sent: deque[float] = deque()
        self._task: asyncio.Task | None = None

    def send(self, text: str, severity: str = "INFO") -> None:
        try:
            self._q.put_nowait(text[:3900])
        except asyncio.QueueFull:
            METRICS.inc("alerts.dropped")

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                text = await self._q.get()
                now = time.time()
                while self._sent and self._sent[0] < now - 60:
                    self._sent.popleft()
                if len(self._sent) >= self.max_per_minute:
                    await asyncio.sleep(60 - (now - self._sent[0]))
                for attempt in range(3):
                    try:
                        r = await client.post(
                            self._url, json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
                        )
                        if r.status_code == 429:
                            await asyncio.sleep(float(r.json().get("parameters", {}).get("retry_after", 5)))
                            continue
                        r.raise_for_status()
                        self._sent.append(time.time())
                        METRICS.inc("alerts.sent")
                        break
                    except httpx.HTTPError as e:
                        log.warning("telegram_send_failed", attempt=attempt, error=type(e).__name__)
                        await asyncio.sleep(2**attempt)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="telegram")
