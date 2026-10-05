"""Wallet / entity analysis.

WalletBook    — per-wallet realized P/L, win rate, holding time and entry timing, built purely from observed trades
                (average-cost accounting, fees included). Historical skill is NOT assumed to persist; it is one
                feature, gated by sample size via a Wilson lower bound.
CreatorBook   — every launch per creator: lifetime, graduation, creator sell behaviour.
EntityGraph   — links wallets that co-buy in the creation slot of several tokens (bundles) or share a funder.
                Links carry evidence counts; clusters are reported with a confidence, never as a verdict.

All statistics are "as of now": they only include trades the engine has already processed, so a replay sees the
same values the live system would have seen.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field

from tradingagent.common.events import MarketEvent
from tradingagent.common.types import EventKind, Side


def wilson_lower(successes: int, n: int, z: float = 1.645) -> float:
    if n <= 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


@dataclass
class _OpenPos:
    qty: int = 0
    cost: int = 0
    realized: int = 0
    opened_at: float = 0.0


@dataclass
class WalletStats:
    trades: int = 0
    buys: int = 0
    sells: int = 0
    realized_pnl: int = 0  # lamports
    closed: int = 0
    wins: int = 0
    hold_time_sum: float = 0.0
    entry_age_sum: float = 0.0
    entries: int = 0
    tokens_touched: int = 0
    last_active: float = 0.0
    open: dict[str, _OpenPos] = field(default_factory=dict)

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.closed if self.closed else None

    @property
    def avg_hold_s(self) -> float | None:
        return self.hold_time_sum / self.closed if self.closed else None

    @property
    def avg_entry_age_s(self) -> float | None:
        return self.entry_age_sum / self.entries if self.entries else None

    def to_dict(self) -> dict:
        return {
            "trades": self.trades,
            "buys": self.buys,
            "sells": self.sells,
            "realized_pnl_sol": self.realized_pnl / 1e9,
            "closed": self.closed,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "win_rate_lower_bound": wilson_lower(self.wins, self.closed),
            "avg_hold_s": self.avg_hold_s,
            "avg_entry_age_s": self.avg_entry_age_s,
            "tokens_touched": self.tokens_touched,
            "open_positions": len(self.open),
        }


class WalletBook:
    def __init__(self, max_wallets: int = 250_000, smart_min_closed: int = 10, smart_min_wilson: float = 0.5) -> None:
        self.wallets: OrderedDict[str, WalletStats] = OrderedDict()
        self.smart: set[str] = set()  # maintained incrementally; membership re-evaluated when a position closes
        self.max_wallets = max_wallets
        self.smart_min_closed, self.smart_min_wilson = smart_min_closed, smart_min_wilson

    def get(self, wallet: str) -> WalletStats | None:
        return self.wallets.get(wallet)

    def on_trade(self, ev: MarketEvent, token_birth: float | None) -> None:
        if not ev.user or ev.kind not in (EventKind.TRADE, EventKind.AMM_TRADE) or not ev.mint:
            return
        w = self.wallets.get(ev.user)
        if w is None:
            w = WalletStats()
            self.wallets[ev.user] = w
            if len(self.wallets) > self.max_wallets:
                evicted, _ = self.wallets.popitem(last=False)
                self.smart.discard(evicted)
        else:
            self.wallets.move_to_end(ev.user)
        w.trades += 1
        w.last_active = ev.t
        pos = w.open.get(ev.mint)
        if ev.side is Side.BUY:
            w.buys += 1
            if pos is None:
                pos = _OpenPos(opened_at=ev.t)
                w.open[ev.mint] = pos
                w.tokens_touched += 1
                if token_birth is not None:
                    w.entry_age_sum += max(0.0, ev.t - token_birth)
                    w.entries += 1
            pos.qty += ev.token_amount
            pos.cost += ev.sol_amount + ev.fee_lamports
        else:
            w.sells += 1
            if pos is None or pos.qty <= 0:
                return  # tokens acquired off-curve (transfer) or before we started observing
            sold = min(ev.token_amount, pos.qty)
            cost_part = pos.cost * sold // pos.qty
            proceeds = ev.sol_amount - ev.fee_lamports
            pnl = proceeds - cost_part
            pos.realized += pnl
            w.realized_pnl += pnl
            pos.qty -= sold
            pos.cost -= cost_part
            if pos.qty <= max(1, sold // 1000):  # closed (dust tolerance)
                w.closed += 1
                if pos.realized > 0:
                    w.wins += 1
                w.hold_time_sum += ev.t - pos.opened_at
                del w.open[ev.mint]
                if self.is_smart(ev.user):
                    self.smart.add(ev.user)
                else:
                    self.smart.discard(ev.user)

    def is_smart(self, wallet: str) -> bool:
        w = self.wallets.get(wallet)
        if w is None or w.closed < self.smart_min_closed or w.realized_pnl <= 0:
            return False
        return wilson_lower(w.wins, w.closed) >= self.smart_min_wilson


@dataclass
class LaunchRecord:
    mint: str
    created_at: float
    last_trade_at: float
    graduated: bool = False
    creator_bought: int = 0
    creator_sold: int = 0
    creator_first_sell_at: float | None = None
    peak_mcap_lamports: int = 0

    @property
    def lifetime_s(self) -> float:
        return max(0.0, self.last_trade_at - self.created_at)

    @property
    def rugged(self) -> bool:
        """Creator sold >= 50% of its buys within 10 minutes of launch."""
        if self.creator_bought <= 0 or self.creator_first_sell_at is None:
            return False
        return self.creator_sold >= 0.5 * self.creator_bought and self.creator_first_sell_at - self.created_at < 600


class CreatorBook:
    def __init__(self) -> None:
        self.launches: dict[str, list[LaunchRecord]] = {}
        self.by_mint: dict[str, LaunchRecord] = {}

    def on_create(self, creator: str, mint: str, t: float) -> None:
        rec = LaunchRecord(mint=mint, created_at=t, last_trade_at=t)
        self.launches.setdefault(creator, []).append(rec)
        self.by_mint[mint] = rec

    def on_trade(self, ev: MarketEvent, creator: str | None, mcap: int | None) -> None:
        rec = self.by_mint.get(ev.mint or "")
        if rec is None:
            return
        rec.last_trade_at = ev.t
        if mcap:
            rec.peak_mcap_lamports = max(rec.peak_mcap_lamports, mcap)
        if creator and ev.user == creator:
            if ev.side is Side.BUY:
                rec.creator_bought += ev.token_amount
            else:
                rec.creator_sold += ev.token_amount
                if rec.creator_first_sell_at is None:
                    rec.creator_first_sell_at = ev.t

    def on_graduate(self, mint: str) -> None:
        rec = self.by_mint.get(mint)
        if rec:
            rec.graduated = True

    def history(self, creator: str | None, exclude_mint: str, now: float) -> dict:
        """Statistics over this creator's OTHER launches created before `now`."""
        prior = [r for r in self.launches.get(creator or "", []) if r.mint != exclude_mint and r.created_at < now]
        n = len(prior)
        if n == 0:
            return {
                "creator_prev_launches": 0,
                "creator_prev_graduation_rate": None,
                "creator_prev_median_lifetime_s": None,
                "creator_prev_rug_rate": None,
                "creator_launches_24h": 0,
            }
        lifetimes = sorted(r.lifetime_s for r in prior)
        return {
            "creator_prev_launches": n,
            "creator_prev_graduation_rate": sum(r.graduated for r in prior) / n,
            "creator_prev_median_lifetime_s": lifetimes[n // 2],
            "creator_prev_rug_rate": sum(r.rugged for r in prior) / n,
            "creator_launches_24h": sum(1 for r in prior if now - r.created_at < 86_400),
        }


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


@dataclass
class EntityLink:
    evidence: int = 0
    kinds: set[str] = field(default_factory=set)


class EntityGraph:
    """Wallet links with evidence counts. A cluster is only formed from links with >= min_evidence."""

    def __init__(self, min_evidence: int = 2) -> None:
        self.links: dict[tuple[str, str], EntityLink] = {}
        self.min_evidence = min_evidence
        self._uf = _UnionFind()
        self.funder: dict[str, str] = {}
        self._clustered: set[str] = set()

    def has_links(self, wallet: str) -> bool:
        return wallet in self._clustered

    def add_evidence(self, a: str, b: str, kind: str) -> None:
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        link = self.links.setdefault(key, EntityLink())
        link.evidence += 1
        link.kinds.add(kind)
        if link.evidence >= self.min_evidence:
            self._uf.union(a, b)
            self._clustered.update((a, b))
        if len(self.links) > 500_000:
            for k in list(self.links)[:100_000]:
                del self.links[k]

    def on_launch_slot_buyers(self, creator: str | None, buyers: set[str]) -> None:
        group = sorted(buyers | ({creator} if creator else set()))
        if len(group) > 30:
            return  # too broad to be meaningful
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                self.add_evidence(a, b, "same_launch_slot")

    def on_funding(self, wallet: str, funder: str) -> None:
        self.funder[wallet] = funder
        self.add_evidence(wallet, funder, "funded_by")
        self.add_evidence(wallet, funder, "funded_by")  # direct funding is strong evidence on its own

    def cluster_of(self, wallet: str) -> str:
        return self._uf.find(wallet)

    def linked(self, a: str, b: str) -> tuple[bool, float]:
        """(linked, confidence). Confidence grows with total evidence: e/(e+2)."""
        if self._uf.find(a) != self._uf.find(b):
            return False, 0.0
        key = (a, b) if a < b else (b, a)
        e = self.links.get(key, EntityLink()).evidence
        e = max(e, self.min_evidence)
        return True, e / (e + 2)


@dataclass
class CreatorAssessment:
    suspicion: float  # 0..1 point estimate
    confidence: float  # 0..1 how much evidence backs it
    reasons: list[str]


def assess_creator(
    history: dict, launch_slot_buyers: int, creator_linked_holding_share: float, bundle_threshold: int
) -> CreatorAssessment:
    """Combine weak heuristics into a suspicion estimate with explicit confidence. Never a binary verdict."""
    reasons: list[str] = []
    n = history.get("creator_prev_launches") or 0
    rug_rate = history.get("creator_prev_rug_rate")
    # Beta(1,3) prior on rug rate (most launches are not immediate rugs by this narrow definition)
    rugs = (rug_rate or 0.0) * n
    post_rug = (1 + rugs) / (4 + n)
    conf_hist = n / (n + 5)
    signals = [(post_rug, conf_hist)]
    if n and rug_rate is not None and rug_rate >= 0.5:
        reasons.append(f"creator rugged {rugs:.0f}/{n} prior launches")
    if (history.get("creator_launches_24h") or 0) >= 5:
        signals.append((0.7, 0.5))
        reasons.append(f"serial launcher: {history['creator_launches_24h']} launches in 24h")
    if launch_slot_buyers >= bundle_threshold:
        signals.append((0.75, min(0.9, launch_slot_buyers / (bundle_threshold * 2))))
        reasons.append(f"{launch_slot_buyers} wallets bought in the creation slot (bundle pattern)")
    if creator_linked_holding_share > 0.1:
        signals.append((0.8, min(0.9, creator_linked_holding_share * 3)))
        reasons.append(f"creator-linked wallets hold {creator_linked_holding_share:.0%} of supply")
    wsum = sum(c for _, c in signals)
    suspicion = sum(s * c for s, c in signals) / wsum if wsum > 0 else 0.25
    confidence = 1 - math.prod(1 - c for _, c in signals)
    return CreatorAssessment(suspicion=round(suspicion, 3), confidence=round(confidence, 3), reasons=reasons)
