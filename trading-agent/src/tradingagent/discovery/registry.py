"""Token registry and lifecycle state machine.

NEW -> BONDING_CURVE -> GRADUATING -> PUMPSWAP -> ACTIVE ; any -> DEAD | UNTRADEABLE ; any -> EXITED (evicted)

Every transition is recorded with time and reason. A token first seen mid-life (no CreateEvent observed) is
flagged `partial_history`: its holder ledger and creator statistics are incomplete, which filters must respect.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tradingagent.common.logging import get_logger
from tradingagent.common.types import LifecycleState as S

log = get_logger("discovery.registry")

_ALLOWED: dict[S, set[S]] = {
    S.NEW: {S.BONDING_CURVE, S.GRADUATING, S.PUMPSWAP, S.DEAD, S.UNTRADEABLE, S.EXITED},
    S.BONDING_CURVE: {S.GRADUATING, S.PUMPSWAP, S.DEAD, S.UNTRADEABLE, S.EXITED},
    S.GRADUATING: {S.PUMPSWAP, S.DEAD, S.UNTRADEABLE, S.EXITED},
    S.PUMPSWAP: {S.ACTIVE, S.DEAD, S.UNTRADEABLE, S.EXITED},
    S.ACTIVE: {S.DEAD, S.UNTRADEABLE, S.EXITED},
    S.DEAD: {S.BONDING_CURVE, S.PUMPSWAP, S.ACTIVE, S.EXITED, S.GRADUATING},  # revival is possible
    S.UNTRADEABLE: {S.EXITED},
    S.EXITED: {S.BONDING_CURVE, S.PUMPSWAP, S.ACTIVE, S.GRADUATING},  # re-discovered after eviction
}


class InvalidTransition(ValueError):
    pass


@dataclass
class TokenRecord:
    mint: str
    first_seen_at: float
    first_seen_slot: int
    state: S = S.NEW
    name: str | None = None  # UNTRUSTED
    symbol: str | None = None  # UNTRUSTED
    uri: str | None = None  # UNTRUSTED
    creator: str | None = None
    created_at: float | None = None  # on-chain create timestamp, if observed
    created_slot: int | None = None
    bonding_curve_address: str | None = None
    pool_address: str | None = None
    graduation_state: str = "NONE"  # NONE | COMPLETE | MIGRATED
    token_program: str | None = None
    quote_mint: str | None = None
    is_mayhem: bool = False
    last_seen_slot: int = 0
    last_seen_at: float = 0.0
    last_trade_at: float = 0.0
    partial_history: bool = False
    untradeable_reason: str | None = None
    history: list[tuple[float, str, str]] = field(default_factory=list)

    @property
    def birth_time(self) -> float:
        return self.created_at if self.created_at else self.first_seen_at

    def age_s(self, now: float) -> float:
        return max(0.0, now - self.birth_time)

    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "uri": self.uri,
            "creator": self.creator,
            "created_at": self.created_at,
            "market_state": self.state.value,
            "bonding_curve_address": self.bonding_curve_address,
            "pool_address": self.pool_address,
            "graduation_state": self.graduation_state,
            "first_seen_slot": self.first_seen_slot,
            "last_seen_slot": self.last_seen_slot,
            "first_seen_at": self.first_seen_at,
            "last_trade_at": self.last_trade_at,
            "partial_history": self.partial_history,
            "token_program": self.token_program,
            "quote_mint": self.quote_mint,
            "is_mayhem": self.is_mayhem,
            "untradeable_reason": self.untradeable_reason,
        }


class TokenRegistry:
    def __init__(self, on_transition: Callable[[TokenRecord, S, S, str, float], None] | None = None) -> None:
        self.tokens: dict[str, TokenRecord] = {}
        self.on_transition = on_transition
        self.creators: dict[str, list[str]] = {}

    def get(self, mint: str) -> TokenRecord | None:
        return self.tokens.get(mint)

    def ensure(self, mint: str, t: float, slot: int, *, from_create: bool) -> tuple[TokenRecord, bool]:
        rec = self.tokens.get(mint)
        if rec is not None:
            return rec, False
        rec = TokenRecord(mint=mint, first_seen_at=t, first_seen_slot=slot, partial_history=not from_create)
        self.tokens[mint] = rec
        return rec, True

    def transition(self, rec: TokenRecord, new: S, t: float, reason: str) -> bool:
        old = rec.state
        if new == old:
            return False
        if new not in _ALLOWED[old]:
            raise InvalidTransition(f"{rec.mint}: {old} -> {new} ({reason})")
        rec.state = new
        rec.history.append((t, new.value, reason))
        if len(rec.history) > 64:
            del rec.history[:32]
        if self.on_transition:
            self.on_transition(rec, old, new, reason, t)
        return True

    def try_transition(self, rec: TokenRecord, new: S, t: float, reason: str) -> bool:
        try:
            return self.transition(rec, new, t, reason)
        except InvalidTransition as e:
            log.warning("invalid_lifecycle_transition", token=rec.mint, error=str(e))
            return False

    def mark_untradeable(self, rec: TokenRecord, t: float, reason: str) -> None:
        rec.untradeable_reason = reason
        self.try_transition(rec, S.UNTRADEABLE, t, reason)

    def register_creator(self, creator: str, mint: str) -> None:
        self.creators.setdefault(creator, []).append(mint)

    def remove(self, mint: str) -> None:
        self.tokens.pop(mint, None)

    def by_state(self, *states: S) -> list[TokenRecord]:
        return [r for r in self.tokens.values() if r.state in states]
