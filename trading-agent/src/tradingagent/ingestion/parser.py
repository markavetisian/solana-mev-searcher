"""Parse Solana transaction logs (logsSubscribe notifications or getTransaction meta) into MarketEvents.

Attribution: Anchor `emit!` writes `Program data: <base64>` while the emitting program is on top of the invoke
stack, so we track `Program <id> invoke [n]` / `Program <id> success|failed` lines and attribute each data line
to the current program. Failed transactions are discarded entirely (their state changes were rolled back).
Truncated logs (`Log truncated`) produce a DATA_GAP event so downstream consumers know the record is incomplete.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from tradingagent.common.events import MarketEvent
from tradingagent.common.logging import get_logger
from tradingagent.common.metrics import METRICS
from tradingagent.common.types import EventKind
from tradingagent.pump.borsh import BorshError, amm_codec, pump_codec
from tradingagent.pump.constants import PUMP_AMM_PROGRAM_ID, PUMP_PROGRAM_ID
from tradingagent.pump.events import from_amm_event, from_pump_event

log = get_logger("ingestion.parser")

PUMP = str(PUMP_PROGRAM_ID)
AMM = str(PUMP_AMM_PROGRAM_ID)
_INVOKE = re.compile(r"^Program (\w+) invoke \[(\d+)\]$")
_EXIT = re.compile(r"^Program (\w+) (success|failed.*)$")
_DATA = "Program data: "


@dataclass
class ParseResult:
    events: list[MarketEvent] = field(default_factory=list)
    failed_tx: bool = False
    truncated: bool = False
    undecodable: int = 0


class LogParser:
    def __init__(self, pool_to_mint: dict[str, str] | None = None) -> None:
        self.pool_to_mint: dict[str, str] = pool_to_mint if pool_to_mint is not None else {}
        self._pump = pump_codec()
        self._amm = amm_codec()

    def parse(
        self,
        signature: str,
        slot: int,
        logs: Iterable[str],
        err: object,
        observed_at: float,
        source: str = "live",
    ) -> ParseResult:
        res = ParseResult()
        if err is not None:
            res.failed_tx = True
            METRICS.inc("ingest.failed_tx_skipped")
            return res
        stack: list[str] = []
        seq = 0
        for line in logs:
            if line.startswith(_DATA):
                program = stack[-1] if stack else None
                payload = line[len(_DATA) :].strip()
                ev = self._decode(program, payload, signature, slot, seq, observed_at, source)
                if ev is None:
                    if program in (PUMP, AMM):
                        res.undecodable += 1
                    continue
                res.events.append(ev)
                seq += 1
                continue
            m = _INVOKE.match(line)
            if m:
                stack.append(m.group(1))
                continue
            m = _EXIT.match(line)
            if m:
                if stack and stack[-1] == m.group(1):
                    stack.pop()
                continue
            if line.startswith("Log truncated"):
                res.truncated = True
        if res.truncated:
            METRICS.inc("ingest.log_truncated")
            res.events.append(
                MarketEvent(
                    kind=EventKind.DATA_GAP,
                    signature=signature,
                    slot=slot,
                    seq=seq,
                    observed_at=observed_at,
                    chain_time=0.0,
                    source=source,
                    extra={"reason": "log_truncated"},
                )
            )
        if res.undecodable:
            METRICS.inc("ingest.undecodable_event", res.undecodable)
        return res

    def _decode(
        self, program: str | None, payload: str, signature: str, slot: int, seq: int, observed_at: float, source: str
    ) -> MarketEvent | None:
        if program not in (PUMP, AMM):
            return None
        try:
            raw = base64.b64decode(payload, validate=True)
        except ValueError:
            return None
        try:
            if program == PUMP:
                decoded = self._pump.decode_event(raw)
                if decoded:
                    return from_pump_event(decoded[0], decoded[1], signature, slot, seq, observed_at, source)
            else:
                decoded = self._amm.decode_event(raw)
                if decoded:
                    return from_amm_event(
                        decoded[0], decoded[1], signature, slot, seq, observed_at, self.pool_to_mint, source
                    )
        except (BorshError, KeyError) as e:
            log.warning("event_decode_failed", signature=signature, slot=slot, program=program, error=str(e))
        return None

    def parse_cpi_event_data(
        self, program: str, data: bytes, signature: str, slot: int, seq: int, observed_at: float, source: str
    ) -> MarketEvent | None:
        """For getTransaction/Geyser sources: decode an emit_cpi inner-instruction payload."""
        b64 = base64.b64encode(data).decode()
        return self._decode(program, b64, signature, slot, seq, observed_at, source)


def parse_logs_notification(parser: LogParser, msg: dict, observed_at: float) -> ParseResult | None:
    """Handle one `logsNotification` JSON-RPC message."""
    params = msg.get("params") or {}
    result = params.get("result") or {}
    ctx = result.get("context") or {}
    value = result.get("value") or {}
    sig = value.get("signature")
    if not sig:
        return None
    return parser.parse(sig, int(ctx.get("slot") or 0), value.get("logs") or [], value.get("err"), observed_at)
