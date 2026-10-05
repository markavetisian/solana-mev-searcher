"""Normalized market event — the single record type that flows through ingestion, storage, replay and features.

Ordering key is (slot, tx_index_hint, seq). `observed_at` is when *this system* saw the event (wall clock, ms
precision); `chain_time` is the on-chain unix timestamp (1 s precision). Live recordings replay on
`observed_at`, so backtests inherit the real ingestion latency instead of pretending we saw events at block time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from tradingagent.common.types import EventKind, Side, Venue


@dataclass(slots=True)
class MarketEvent:
    kind: EventKind
    signature: str
    slot: int
    seq: int  # index of the event within its transaction
    observed_at: float
    chain_time: float
    mint: str | None = None
    venue: Venue | None = None
    side: Side | None = None
    user: str | None = None
    sol_amount: int = 0  # lamports moved through the curve/pool, excluding fees
    token_amount: int = 0
    fee_lamports: int = 0
    creator_fee_lamports: int = 0
    # bonding curve reserves AFTER the trade
    virtual_token_reserves: int | None = None
    virtual_quote_reserves: int | None = None
    real_token_reserves: int | None = None
    real_quote_reserves: int | None = None
    # PumpSwap reserves AFTER the trade
    pool: str | None = None
    pool_base_reserves: int | None = None
    pool_quote_reserves: int | None = None
    pool_virtual_quote_reserves: int | None = None
    creator: str | None = None
    name: str | None = None  # UNTRUSTED
    symbol: str | None = None  # UNTRUSTED
    uri: str | None = None  # UNTRUSTED
    token_program: str | None = None
    quote_mint: str | None = None
    is_mayhem: bool = False
    source: str = "live"  # live | backfill | replay | synthetic
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def t(self) -> float:
        return self.observed_at

    @property
    def order_key(self) -> tuple[float, int, str, int]:
        return (self.observed_at, self.slot, self.signature, self.seq)

    @property
    def event_id(self) -> str:
        return f"{self.signature}:{self.seq}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["venue"] = self.venue.value if self.venue else None
        d["side"] = self.side.value if self.side else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MarketEvent:
        d = dict(d)
        d["kind"] = EventKind(d["kind"])
        d["venue"] = Venue(d["venue"]) if d.get("venue") else None
        d["side"] = Side(d["side"]) if d.get("side") else None
        known = cls.__dataclass_fields__.keys()
        extra = {k: v for k, v in d.items() if k not in known}
        out = cls(**{k: v for k, v in d.items() if k in known})
        if extra:
            out.extra.update(extra)
        return out

    @property
    def price_after(self) -> float | None:
        """Spot price after the event, lamports per token base unit."""
        if self.venue is Venue.BONDING_CURVE and self.virtual_token_reserves:
            return (self.virtual_quote_reserves or 0) / self.virtual_token_reserves
        if self.venue is Venue.PUMPSWAP and self.pool_base_reserves:
            q = (self.pool_quote_reserves or 0) + (self.pool_virtual_quote_reserves or 0)
            return q / self.pool_base_reserves
        return None
