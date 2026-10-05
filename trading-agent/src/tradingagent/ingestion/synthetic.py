"""SYNTHETIC market generator — for tests, demos and pipeline validation ONLY.

Every event is tagged source="synthetic". Synthetic results say NOTHING about real-market edge, and the
backtester refuses synthetic data unless explicitly allowed.

Its real purpose is as a *control experiment* for the research and backtest machinery:
  * planted_edge = 0.0  -> negative control: order flow is memoryless, so no strategy should show an
                           after-cost edge. If the pipeline reports one, the pipeline is broken (leakage / bias).
  * planted_edge > 0.0  -> positive control: price drift persists within phases and "smart" wallets front-run
                           positive phases, so a correct pipeline should detect predictive power.
Reserves always follow the exact bonding-curve / AMM math, so execution-cost modelling is realistic.
"""

from __future__ import annotations

import hashlib
import heapq
import random
from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from tradingagent.common.events import MarketEvent
from tradingagent.common.types import EventKind, Side, Venue
from tradingagent.pump.amm import AmmError, PoolState, quote_buy_quote_in, quote_sell_base_in
from tradingagent.pump.constants import WSOL_MINT, canonical_pool_pda
from tradingagent.pump.curve import (
    BondingCurveState,
    CurveError,
    initial_state,
    quote_buy_exact_quote_in,
    quote_sell,
)
from tradingagent.pump.fees import FeeSchedule

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _fake_key(rng: random.Random, suffix: str = "") -> str:
    # Real 32-byte keys so downstream PDA derivations work.
    return str(Pubkey.from_bytes(hashlib.sha256(f"{rng.random()}{suffix}".encode()).digest()))


@dataclass
class _Token:
    mint: str
    creator: str
    created_at: float
    curve: BondingCurveState
    pool: PoolState | None = None
    pool_addr: str | None = None
    holders: dict[str, int] = field(default_factory=dict)
    drift: float = 0.0
    phase_end: float = 0.0
    rate: float = 0.3
    rug_at: float | None = None
    dies_at: float = 0.0
    sell_debt_sol: float = 0.0  # null mode: sell pressure that could not be filled yet carries forward
    graduated: bool = False
    dead: bool = False


class SyntheticMarket:
    def __init__(
        self,
        seed: int = 7,
        planted_edge: float = 0.0,
        launches_per_min: float = 4.0,
        duration_s: float = 3_600.0,
        start_time: float = 1_760_000_000.0,
        n_wallets: int = 800,
        fee_schedule: FeeSchedule | None = None,
        rug_rate: float | None = None,
    ) -> None:
        from tradingagent.common.config import PumpConfig

        pc = PumpConfig()
        self.schedule = fee_schedule or FeeSchedule.from_rows(pc.fee_tiers, pc.flat_fees)
        self.rng = random.Random(seed)
        self.edge = planted_edge
        # Rugs are a predictable effect (creator holdings -> future dump). The negative control disables them.
        self.rug_rate = (0.25 if planted_edge > 0 else 0.0) if rug_rate is None else rug_rate
        self.launch_rate = launches_per_min / 60.0
        self.duration = duration_s
        self.t0 = start_time
        self.wallets = [_fake_key(self.rng, f"w{i}") for i in range(n_wallets)]
        self.smart = set(self.wallets[: max(5, n_wallets // 40)])
        self._slot0 = 300_000_000
        self._sig = 0

    def _sig_next(self) -> str:
        self._sig += 1
        return f"synthetic-{self._sig:012d}"

    def _slot(self, t: float) -> int:
        return self._slot0 + int((t - self.t0) / 0.4)

    def _ev(self, t: float, **kw) -> MarketEvent:
        return MarketEvent(
            signature=self._sig_next(),
            slot=self._slot(t),
            seq=0,
            observed_at=t + 0.35,
            chain_time=float(int(t)),
            source="synthetic",
            **kw,
        )

    def generate(self) -> list[MarketEvent]:
        rng = self.rng
        out: list[MarketEvent] = []
        heap: list[tuple[float, int, str]] = []
        tokens: dict[str, _Token] = {}
        t, n = self.t0, 0
        while True:
            t += rng.expovariate(self.launch_rate)
            if t > self.t0 + self.duration:
                break
            n += 1
            mint, creator = _fake_key(rng, f"m{n}"), rng.choice(self.wallets[len(self.smart) :])
            tok = _Token(mint=mint, creator=creator, created_at=t, curve=initial_state(creator=creator))
            tok.dies_at = t + rng.expovariate(1 / 600.0)
            if rng.random() < self.rug_rate:
                tok.rug_at = t + rng.uniform(20, 400)
            tokens[mint] = tok
            out.append(
                self._ev(
                    t,
                    kind=EventKind.CREATE,
                    mint=mint,
                    venue=Venue.BONDING_CURVE,
                    user=creator,
                    creator=creator,
                    name=f"SYN{n}",
                    symbol=f"S{n}",
                    uri="https://example.invalid/meta.json",
                    virtual_token_reserves=tok.curve.virtual_token_reserves,
                    virtual_quote_reserves=tok.curve.virtual_quote_reserves,
                    real_token_reserves=tok.curve.real_token_reserves,
                    real_quote_reserves=0,
                    quote_mint=str(WSOL_MINT),
                )
            )
            # creator dev-buy
            self._trade(out, tok, t + 0.01, creator, Side.BUY, rng.uniform(0.2, 2.5))
            heapq.heappush(heap, (t + 0.05 + rng.expovariate(1.0), n, mint))
        end = self.t0 + self.duration
        while heap:
            t, k, mint = heapq.heappop(heap)
            if t > end:
                continue
            tok = tokens[mint]
            if tok.dead:
                continue
            if t >= tok.phase_end:
                mag = abs(rng.gauss(0, 0.25))
                tok.drift = max(-0.4, min(0.4, rng.choice((-1, 1)) * mag))
                tok.phase_end = t + rng.uniform(20, 240)
                base_rate = rng.uniform(0.05, 1.2)
                tok.rate = base_rate * (1 + 3 * self.edge * max(tok.drift, 0)) if self.edge else base_rate
            if tok.rug_at is not None and t >= tok.rug_at:
                bal = tok.holders.get(tok.creator, 0)
                if bal > 0:
                    self._trade(out, tok, t, tok.creator, Side.SELL, tokens_amt=bal)
                tok.rug_at = None
                tok.drift, tok.phase_end = -0.35, t + 120
            if t >= tok.dies_at and not tok.graduated:
                tok.rate *= 0.3
                if tok.rate < 0.02:
                    tok.dead = True
                    continue
            p_buy = 0.5 + self.edge * tok.drift
            size_sol = min(rng.lognormvariate(-1.2, 1.0), 8.0)
            if self.edge == 0.0:
                self._null_step(out, tok, t, size_sol, rng)
            else:
                smart_buy = tok.drift > 0.1 and rng.random() < 0.3 * self.edge
                if smart_buy:
                    self._trade(out, tok, t, rng.choice(list(self.smart)), Side.BUY, size_sol)
                elif rng.random() < p_buy or not tok.holders:
                    self._trade(out, tok, t, rng.choice(self.wallets), Side.BUY, size_sol)
                else:
                    user = rng.choice(list(tok.holders))
                    bal = tok.holders.get(user, 0)
                    frac = 1.0 if rng.random() < 0.5 else rng.uniform(0.2, 0.9)
                    if bal > 0:
                        self._trade(out, tok, t, user, Side.SELL, tokens_amt=max(1, int(bal * frac)))
            heapq.heappush(heap, (t + rng.expovariate(max(tok.rate, 0.01)), k, mint))
        out.sort(key=lambda e: e.order_key)
        return out

    def _null_step(self, out: list[MarketEvent], tok: _Token, t: float, size_sol: float, rng: random.Random) -> None:
        """Negative control: buys and sells of identically distributed SOL size with probability 1/2 each.
        Sell pressure that cannot be filled (not enough tokens in circulation) is carried forward and filled first,
        so there is no systematic drift and no feature can legitimately predict after-cost returns."""
        if rng.random() < 0.5:
            self._trade(out, tok, t, rng.choice(self.wallets), Side.BUY, size_sol)
            return
        want = size_sol + tok.sell_debt_sol
        px = tok.pool.spot_price if tok.pool else tok.curve.spot_price
        for user in sorted(tok.holders, key=lambda u: -tok.holders[u])[:5]:
            if want <= 1e-6:
                break
            bal = tok.holders.get(user, 0)
            amt = min(bal, max(1, int(want * 1e9 / px)))
            if amt <= 0:
                continue
            self._trade(out, tok, t, user, Side.SELL, tokens_amt=amt)
            want -= amt * px / 1e9
        tok.sell_debt_sol = max(0.0, want)

    def _trade(
        self,
        out: list[MarketEvent],
        tok: _Token,
        t: float,
        user: str,
        side: Side,
        sol: float = 0.0,
        tokens_amt: int = 0,
    ) -> None:
        try:
            if tok.pool is None:
                if side is Side.BUY:
                    q = quote_buy_exact_quote_in(tok.curve, int(sol * 1e9), self.schedule)
                else:
                    q = quote_sell(tok.curve, tokens_amt, self.schedule)
            elif side is Side.BUY:
                q = quote_buy_quote_in(tok.pool, int(sol * 1e9), self.schedule)
            else:
                q = quote_sell_base_in(tok.pool, tokens_amt, self.schedule)
        except (CurveError, AmmError):
            return
        delta = q.amount_out if side is Side.BUY else -q.amount_in
        tok.holders[user] = tok.holders.get(user, 0) + delta
        if tok.holders[user] <= 0:
            tok.holders.pop(user, None)
        if tok.pool is None:
            s = q.state_after
            tok.curve = s  # type: ignore[assignment]
            out.append(
                self._ev(
                    t,
                    kind=EventKind.TRADE,
                    mint=tok.mint,
                    venue=Venue.BONDING_CURVE,
                    side=side,
                    user=user,
                    sol_amount=q.quote_amount_gross,
                    token_amount=q.token_amount,
                    fee_lamports=q.total_fee,
                    creator_fee_lamports=q.creator_fee,
                    virtual_token_reserves=s.virtual_token_reserves,
                    virtual_quote_reserves=s.virtual_quote_reserves,
                    real_token_reserves=s.real_token_reserves,
                    real_quote_reserves=s.real_quote_reserves,
                    creator=tok.creator,
                    quote_mint=str(WSOL_MINT),
                )
            )
            if s.complete and not tok.graduated:
                self._graduate(out, tok, t)
        else:
            p = q.state_after
            tok.pool = p  # type: ignore[assignment]
            out.append(
                self._ev(
                    t,
                    kind=EventKind.AMM_TRADE,
                    mint=tok.mint,
                    venue=Venue.PUMPSWAP,
                    side=side,
                    user=user,
                    sol_amount=q.quote_amount_gross,
                    token_amount=q.token_amount,
                    fee_lamports=q.total_fee,
                    pool=tok.pool_addr,
                    pool_base_reserves=p.base_reserve,
                    pool_quote_reserves=p.quote_reserve,
                    pool_virtual_quote_reserves=0,
                    creator=tok.creator,
                )
            )

    def _graduate(self, out: list[MarketEvent], tok: _Token, t: float) -> None:
        tok.graduated = True
        out.append(self._ev(t + 0.01, kind=EventKind.COMPLETE, mint=tok.mint, venue=Venue.BONDING_CURVE))
        pool = str(canonical_pool_pda(Pubkey.from_string(tok.mint)))
        quote = tok.curve.real_quote_reserves - 15_000_001
        base = 206_900_000_000_000
        tok.pool, tok.pool_addr = PoolState(base_reserve=base, quote_reserve=quote), pool
        out.append(
            self._ev(
                t + 1.0,
                kind=EventKind.MIGRATE,
                mint=tok.mint,
                venue=Venue.PUMPSWAP,
                pool=pool,
                sol_amount=quote,
                token_amount=base,
            )
        )
        out.append(
            self._ev(
                t + 1.0,
                kind=EventKind.POOL_CREATE,
                mint=tok.mint,
                venue=Venue.PUMPSWAP,
                pool=pool,
                pool_base_reserves=base,
                pool_quote_reserves=quote,
                pool_virtual_quote_reserves=0,
                creator=tok.creator,
                quote_mint=str(WSOL_MINT),
            )
        )
