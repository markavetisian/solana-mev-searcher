"""Multi-timeframe feature engine.

Pure function of (token market state, wallet/creator books, as_of). Every value is computed from trades with
t <= as_of; missing/undefined values are None (never silently 0). Feature names are stable and versioned by
versions.FEATURE_VERSION — change a definition, bump the version.

Windows: 5s, 15s, 30s, 1m, 5m, 15m, 1h.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from tradingagent.common.sanitize import looks_like_injection
from tradingagent.common.types import Venue
from tradingagent.common.units import LAMPORTS_PER_SOL, PUMP_TOTAL_SUPPLY
from tradingagent.common.versions import FEATURE_VERSION
from tradingagent.market.series import LookAheadError, Trade
from tradingagent.market.token import TokenMarket
from tradingagent.pump.adapters import QuoteError
from tradingagent.pump.fees import FeeSchedule
from tradingagent.wallets.entities import CreatorBook, EntityGraph, WalletBook, assess_creator

WINDOWS: tuple[int, ...] = (5, 15, 30, 60, 300, 900, 3600)
_SOL = LAMPORTS_PER_SOL
INITIAL_CURVE_PRICE = 30_000_000_000 / 1_073_000_000_000_000


@dataclass
class FeatureVector:
    mint: str
    as_of: float
    values: dict[str, float | None]
    version: str = FEATURE_VERSION
    meta: dict = field(default_factory=dict)

    def get(self, name: str, default: float | None = None) -> float | None:
        v = self.values.get(name, default)
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return default
        return v


def _pstdev(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _sum_sol(rows: list[Trade], side: int | None = None) -> float:
    return sum(r.sol for r in rows if side is None or r.side == side) / _SOL


def _ratio(a: float, b: float, eps: float) -> float:
    return (a + eps) / (b + eps)


def _bucket_prices(rows: list[Trade], start: float, end: float, step: float, p0: float | None) -> list[float]:
    """Last price in each `step` bucket over (start, end], forward-filled; p0 seeds the first bucket."""
    n = max(1, int(round((end - start) / step)))
    out: list[float] = []
    last = p0
    i = 0
    for b in range(1, n + 1):
        edge = start + b * step
        while i < len(rows) and rows[i].t <= edge:
            last = rows[i].price
            i += 1
        if last is not None:
            out.append(last)
    return out


class FeatureEngine:
    def __init__(
        self,
        schedule: FeeSchedule,
        wallets: WalletBook,
        creators: CreatorBook,
        entities: EntityGraph,
        ref_size_sol: float = 0.25,
        bundle_threshold: int = 6,
    ) -> None:
        self.schedule, self.wallets, self.creators, self.entities = schedule, wallets, creators, entities
        self.ref_size_sol = ref_size_sol
        self.bundle_threshold = bundle_threshold

    def compute(self, tm: TokenMarket, as_of: float, global_staleness_s: float | None = None) -> FeatureVector:
        f: dict[str, float | None] = {}
        s = tm.series
        rec = tm.record
        if as_of + 1e-6 < max(rec.last_seen_at, s.last_t):
            # The token's state (reserves, holders) already reflects events after `as_of`: computing "as of" an
            # earlier time would leak the future into the features.
            raise LookAheadError(f"{rec.mint}: as_of {as_of} precedes ingested state at {rec.last_seen_at}")
        px_now = tm.spot_price()
        f["price"] = px_now
        f["age_s"] = rec.age_s(as_of)
        f["partial_history"] = 1.0 if rec.partial_history else 0.0
        f["trade_count_total"] = float(s.total_count)
        f["since_last_trade_s"] = (as_of - rec.last_trade_at) if rec.last_trade_at else None
        f["since_data_gap_s"] = (as_of - tm.last_gap_at) if tm.last_gap_at else None
        f["reserve_mismatches"] = float(tm.reserve_mismatches)
        f["data_staleness_s"] = global_staleness_s
        f["venue_pumpswap"] = 1.0 if tm.venue is Venue.PUMPSWAP else 0.0
        f["progress"] = tm.curve.progress if tm.curve is not None and tm.venue is Venue.BONDING_CURVE else None
        mcap = tm.market_cap_lamports()
        f["mcap_sol"] = mcap / _SOL if mcap else None
        liq = tm.liquidity_lamports() / _SOL
        f["liquidity_sol"] = liq
        f["liq_mcap_ratio"] = (liq / f["mcap_sol"]) if f["mcap_sol"] else None

        # ---- per-window momentum / volume / flow ----------------------------------------------------------
        for w in WINDOWS:
            bl, sl, nt = s.volume_between(as_of - w, as_of)
            pbl, psl, _ = s.volume_between(as_of - 2 * w, as_of - w)
            ref = s.last_before(as_of - w)
            p_then = (
                ref.price
                if ref
                else (
                    INITIAL_CURVE_PRICE
                    if (not rec.partial_history and rec.created_at and rec.created_at > as_of - w)
                    else None
                )
            )
            f[f"ret_{w}s"] = (px_now / p_then - 1) if (px_now and p_then) else None
            ref2 = s.last_before(as_of - 2 * w)
            if p_then and ref2 and f[f"ret_{w}s"] is not None:
                f[f"price_accel_{w}s"] = f[f"ret_{w}s"] - (p_then / ref2.price - 1)
            else:
                f[f"price_accel_{w}s"] = None
            bv, sv = bl / _SOL, sl / _SOL
            f[f"vol_sol_{w}s"] = bv + sv
            f[f"buy_vol_{w}s"] = bv
            f[f"sell_vol_{w}s"] = sv
            f[f"imbalance_{w}s"] = (bv - sv) / (bv + sv) if (bv + sv) > 0 else None
            f[f"n_trades_{w}s"] = float(nt)
            f[f"tx_per_min_{w}s"] = nt * 60.0 / w
            if w <= 900:
                eps = 0.05 if w <= 30 else 0.2
                pbv, psv = pbl / _SOL, psl / _SOL
                f[f"vol_accel_{w}s"] = _ratio(bv + sv, pbv + psv, eps)
                f[f"buy_vol_accel_{w}s"] = _ratio(bv, pbv, eps)
                f[f"sell_vol_accel_{w}s"] = _ratio(sv, psv, eps)
            if w <= 300:  # unique-participant counts need a scan; keep them to short windows
                cur = s.window(as_of, w)
                prev = s.between(as_of - 2 * w, as_of - w)
                buyers = {r.user for r in cur if r.side > 0}
                f[f"buyers_{w}s"] = float(len(buyers))
                f[f"sellers_{w}s"] = float(len({r.user for r in cur if r.side < 0}))
                pb = {r.user for r in prev if r.side > 0}
                f[f"buyer_growth_{w}s"] = _ratio(len(buyers), len(pb), 1.0) - 1
        f["buyer_seller_ratio_60s"] = _ratio(f["buyers_60s"] or 0, f["sellers_60s"] or 0, 1.0)
        f["unique_traders_300s"] = float(len({r.user for r in s.window(as_of, 300)}))

        # ---- structure / volatility ----------------------------------------------------------------------
        for w in (60, 300, 900):
            rows = s.window(as_of, w)
            ref = s.last_before(as_of - w)
            prices = _bucket_prices(rows, as_of - w, as_of, 5.0, ref.price if ref else None)
            rets = [math.log(b / a) for a, b in zip(prices, prices[1:], strict=False) if a > 0 and b > 0]
            f[f"volatility_{w}s"] = _pstdev(rets) if len(rets) >= 3 else None
            if w == 60:
                moves = [r for r in rets if r != 0]
                f["persistence_60s"] = (sum(1 for r in moves if r > 0) / len(moves)) if len(moves) >= 2 else None
        rows60 = s.window(as_of, 60)
        f["hh_hl_score_60s"] = self._hh_hl(rows60, as_of)

        # ---- trade-size distribution & bursts --------------------------------------------------------------
        rows300 = s.window(as_of, 300)
        buy_sizes = sorted(r.sol / _SOL for r in rows300 if r.side > 0)
        sell_sizes = sorted(r.sol / _SOL for r in rows300 if r.side < 0)
        f["median_buy_sol_300s"] = statistics.median(buy_sizes) if buy_sizes else None
        f["p90_buy_sol_300s"] = buy_sizes[int(0.9 * (len(buy_sizes) - 1))] if buy_sizes else None
        big_buy = max(f["p90_buy_sol_300s"] or 0.0, 1.0)
        big_sell = max(sell_sizes[int(0.9 * (len(sell_sizes) - 1))] if sell_sizes else 0.0, 1.0)
        rows15 = s.window(as_of, 15)
        f["buy_burst_15s"] = float(sum(1 for r in rows15 if r.side > 0 and r.sol / _SOL >= big_buy))
        f["sell_burst_15s"] = float(sum(1 for r in rows15 if r.side < 0 and r.sol / _SOL >= big_sell))
        mc = f["mcap_sol"]
        f["vol_mcap_ratio_300s"] = (f["vol_sol_300s"] / mc) if mc else None
        f["vol_liq_ratio_300s"] = (f["vol_sol_300s"] / liq) if liq > 0 else None

        # ---- liquidity dynamics ------------------------------------------------------------------------------
        r60 = s.last_before(as_of - 60)
        f["liq_change_60s"] = (tm.liquidity_lamports() / r60.liquidity - 1) if (r60 and r60.liquidity > 0) else None
        f["liq_drawdown_from_peak"] = (1 - tm.liquidity_lamports() / tm.peak_liquidity) if tm.peak_liquidity else None

        # ---- execution / exit quality for the reference size --------------------------------------------------
        self._exec_features(tm, f)

        # ---- holders -----------------------------------------------------------------------------------------
        self._holder_features(tm, as_of, f)

        # ---- creator / entities ------------------------------------------------------------------------------
        hist = self.creators.history(rec.creator, rec.mint, as_of)
        f.update({k: (float(v) if v is not None else None) for k, v in hist.items()})
        creator_bal = tm.creator_balance
        f["creator_holding"] = creator_bal / PUMP_TOTAL_SUPPLY if not rec.partial_history else None
        f["creator_sold_frac"] = (
            (tm.creator_sold / tm.creator_bought) if tm.creator_bought else (0.0 if not rec.partial_history else None)
        )
        f["creator_has_sold"] = 1.0 if tm.creator_first_sell_at is not None else 0.0
        f["launch_slot_buyers"] = float(len(tm.launch_slot_buyers))
        f["sniper_supply_share"] = tm.sniper_tokens / PUMP_TOTAL_SUPPLY if not rec.partial_history else None
        linked_share = 0.0
        if rec.creator and self.entities.has_links(rec.creator):
            for user, bal in tm.holders.items():
                if user != rec.creator and self.entities.linked(user, rec.creator)[0]:
                    linked_share += bal / PUMP_TOTAL_SUPPLY
        f["creator_linked_holding"] = linked_share
        ca = assess_creator(hist, len(tm.launch_slot_buyers), linked_share, self.bundle_threshold)
        f["creator_suspicion"] = ca.suspicion
        f["creator_suspicion_confidence"] = ca.confidence

        # ---- wallets -----------------------------------------------------------------------------------------
        buys300 = [r for r in rows300 if r.side > 0]
        tot = sum(r.sol for r in buys300)
        smart_set = self.wallets.smart
        smart = sum(r.sol for r in buys300 if r.user in smart_set)
        f["smart_wallet_buy_share_300s"] = (smart / tot) if tot > 0 else None
        f["metadata_injection"] = 1.0 if looks_like_injection(rec.name, rec.symbol, rec.uri) else 0.0

        return FeatureVector(
            mint=rec.mint,
            as_of=as_of,
            values=f,
            meta={
                "creator_reasons": ca.reasons,
                "state": rec.state.value,
                "venue": tm.venue.value if tm.venue else None,
            },
        )

    # ---------------------------------------------------------------------------------------------------------
    @staticmethod
    def _hh_hl(rows: list[Trade], as_of: float) -> float | None:
        if len(rows) < 4:
            return None
        buckets: list[list[float]] = [[] for _ in range(4)]
        for r in rows:
            i = min(3, int((r.t - (as_of - 60)) // 15))
            if i >= 0:
                buckets[i].append(r.price)
        hl = [(max(b), min(b)) for b in buckets if b]
        if len(hl) < 3:
            return None
        score = sum((b[0] > a[0]) + (b[1] > a[1]) for a, b in zip(hl, hl[1:], strict=False))
        return score / (2 * (len(hl) - 1))

    def _exec_features(self, tm: TokenMarket, f: dict) -> None:
        adapter = tm.adapter(self.schedule)
        f["entry_impact_frac"] = f["entry_cost_frac"] = f["exit_impact_frac"] = f["roundtrip_cost_frac"] = None
        if adapter is None:
            return
        spend = int(self.ref_size_sol * _SOL)
        try:
            buy = adapter.quote_buy(spend)
            sell = adapter.after(buy).quote_sell(buy.amount_out)
        except QuoteError:
            return
        f["entry_impact_frac"] = buy.price_impact
        f["entry_cost_frac"] = buy.all_in_slippage
        f["exit_impact_frac"] = sell.all_in_slippage
        f["roundtrip_cost_frac"] = 1 - sell.amount_out / buy.amount_in

    def _holder_features(self, tm: TokenMarket, as_of: float, f: dict) -> None:
        rec = tm.record
        snap = tm.holder_snapshot
        use_snapshot = snap is not None and as_of - snap.t < 300 and snap.t <= as_of
        if rec.partial_history and not use_snapshot:
            for k in (
                "holders",
                "holder_growth_60s",
                "new_holder_rate_60s",
                "holder_retention",
                "top10_share",
                "top20_share",
                "whale_net_flow_300s",
                "max_holder_share",
            ):
                f[k] = None
            return
        h_now = len(tm.holders)
        h_60 = tm.holder_count_at(as_of - 60)
        f["holders"] = float(h_now)
        f["holder_growth_60s"] = (h_now - h_60) / max(h_60, 1)
        f["new_holder_rate_60s"] = float(sum(1 for t in tm.first_buy_at.values() if as_of - 60 < t <= as_of))
        eb = tm.early_buyers
        f["holder_retention"] = (sum(1 for u in eb if u in tm.holders) / len(eb)) if len(eb) >= 5 else None
        if use_snapshot:
            bals = sorted(snap.top_balances, reverse=True)  # type: ignore[union-attr]
        else:
            bals = sorted(tm.holders.values(), reverse=True)
        f["top10_share"] = sum(bals[:10]) / PUMP_TOTAL_SUPPLY
        f["top20_share"] = sum(bals[:20]) / PUMP_TOTAL_SUPPLY
        f["max_holder_share"] = (bals[0] / PUMP_TOTAL_SUPPLY) if bals else 0.0
        whales = {u for u, b in tm.holders.items() if b >= 0.01 * PUMP_TOTAL_SUPPLY}
        net = sum(r.side * r.tokens for r in tm.series.window(as_of, 300) if r.user in whales)
        f["whale_net_flow_300s"] = net / PUMP_TOTAL_SUPPLY
