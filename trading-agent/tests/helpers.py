"""Test helpers: deterministic market construction with exact curve math."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from tradingagent.common.config import AppConfig
from tradingagent.common.events import MarketEvent
from tradingagent.common.types import EventKind, Side, Venue
from tradingagent.pump.constants import WSOL_MINT
from tradingagent.pump.curve import BondingCurveState, initial_state, quote_buy_exact_quote_in, quote_sell
from tradingagent.pump.fees import FeeSchedule


def key(label: str) -> str:
    return str(Pubkey.from_bytes(hashlib.sha256(label.encode()).digest()))


def schedule(cfg: AppConfig | None = None) -> FeeSchedule:
    cfg = cfg or AppConfig()
    return FeeSchedule.from_rows(cfg.pump.fee_tiers, cfg.pump.flat_fees)


@dataclass
class CurveToken:
    """Builds a realistic event stream for one bonding-curve token."""

    name: str
    t0: float
    slot0: int = 300_000_000
    sched: FeeSchedule = field(default_factory=schedule)
    source: str = "test"

    def __post_init__(self) -> None:
        self.mint = key(f"mint-{self.name}")
        self.creator = key(f"creator-{self.name}")
        self.state: BondingCurveState = initial_state(creator=self.creator)
        self.events: list[MarketEvent] = []
        self.holdings: dict[str, int] = {}
        self._n = 0

    def _ev(self, t: float, slot: int, **kw) -> MarketEvent:
        self._n += 1
        return MarketEvent(
            signature=f"{self.name}-{self._n:08d}",
            slot=slot,
            seq=0,
            observed_at=t,
            chain_time=float(int(t)),
            source=self.source,
            **kw,
        )

    def create(self, symbol: str | None = None, uri: str = "https://x.invalid/m.json") -> CurveToken:
        self.events.append(
            self._ev(
                self.t0,
                self.slot0,
                kind=EventKind.CREATE,
                mint=self.mint,
                venue=Venue.BONDING_CURVE,
                user=self.creator,
                creator=self.creator,
                name=self.name,
                symbol=symbol or self.name[:6],
                uri=uri,
                virtual_token_reserves=self.state.virtual_token_reserves,
                virtual_quote_reserves=self.state.virtual_quote_reserves,
                real_token_reserves=self.state.real_token_reserves,
                real_quote_reserves=0,
                quote_mint=str(WSOL_MINT),
            )
        )
        return self

    def buy(self, t: float, user: str, sol: float, slot: int | None = None) -> CurveToken:
        q = quote_buy_exact_quote_in(self.state, int(sol * 1e9), self.sched)
        self._apply(t, user, Side.BUY, q, slot)
        return self

    def sell(
        self, t: float, user: str, tokens: int | None = None, frac: float = 1.0, slot: int | None = None
    ) -> CurveToken:
        amt = tokens if tokens is not None else int(self.holdings.get(user, 0) * frac)
        if amt <= 0:
            return self
        q = quote_sell(self.state, amt, self.sched)
        self._apply(t, user, Side.SELL, q, slot)
        return self

    def _apply(self, t: float, user: str, side: Side, q, slot: int | None) -> None:
        s = q.state_after
        self.state = s
        delta = q.amount_out if side is Side.BUY else -q.amount_in
        self.holdings[user] = self.holdings.get(user, 0) + delta
        sl = slot if slot is not None else self.slot0 + 3 + int((t - self.t0) / 0.4)
        self.events.append(
            self._ev(
                t,
                sl,
                kind=EventKind.TRADE,
                mint=self.mint,
                venue=Venue.BONDING_CURVE,
                side=side,
                user=user,
                sol_amount=q.quote_amount_gross,
                token_amount=q.token_amount,
                fee_lamports=q.total_fee,
                virtual_token_reserves=s.virtual_token_reserves,
                virtual_quote_reserves=s.virtual_quote_reserves,
                real_token_reserves=s.real_token_reserves,
                real_quote_reserves=s.real_quote_reserves,
                creator=self.creator,
                quote_mint=str(WSOL_MINT),
            )
        )

    def pump(
        self,
        t_start: float,
        t_end: float,
        every: float = 1.0,
        sol: float = 0.4,
        n_users: int = 40,
        sell_every: int = 4,
        prefix: str = "u",
    ) -> CurveToken:
        """Steady buying with occasional partial sells: rising price, growing holders."""
        t, i = t_start, 0
        while t < t_end:
            u = key(f"{self.name}-{prefix}{i % n_users}")
            if i % sell_every == sell_every - 1 and self.holdings.get(u, 0) > 0:
                self.sell(t, u, frac=0.5)
            else:
                self.buy(t, u, sol)
            t += every
            i += 1
        return self

    def dump(self, t_start: float, t_end: float, every: float = 1.0) -> CurveToken:
        t = t_start
        for u in sorted(self.holdings, key=lambda x: -self.holdings[x]):
            if t >= t_end:
                break
            if self.holdings[u] > 0:
                self.sell(t, u, frac=1.0)
                t += every
        return self
