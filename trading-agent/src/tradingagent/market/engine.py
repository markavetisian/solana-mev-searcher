"""Market data engine: consumes normalized events, maintains per-token state, integrity checks and global stats.

Integrity: every curve trade is checked against the previous reserves. Pump TradeEvents carry reserves *after*
the trade, so prev_virtual_token -/+ token_amount must equal the new virtual_token reserves. A mismatch means
we missed events for that token (dropped websocket message, truncated logs) and the token is flagged so the
data-validation gate rejects it until enough clean history accumulates.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from tradingagent.common.clock import Clock
from tradingagent.common.config import AppConfig
from tradingagent.common.events import MarketEvent
from tradingagent.common.logging import get_logger
from tradingagent.common.metrics import METRICS
from tradingagent.common.types import EventKind, Side, Venue
from tradingagent.common.types import LifecycleState as S
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.discovery.registry import TokenRecord, TokenRegistry
from tradingagent.market.series import Trade, TradeSeries
from tradingagent.market.token import HolderSnapshot, TokenMarket
from tradingagent.pump.amm import PoolState
from tradingagent.pump.constants import WSOL_MINT
from tradingagent.pump.curve import BondingCurveState
from tradingagent.pump.fees import FeeSchedule

log = get_logger("market.engine")
_WSOL = str(WSOL_MINT)
_TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class GlobalActivity:
    """Rolling market-wide activity used by the regime detector and health checks (O(1) amortized updates)."""

    window_s: float = 300.0
    trades: deque[tuple[float, float, int]] = field(default_factory=deque)  # (t, sol, side)
    launches: deque[float] = field(default_factory=deque)
    graduations: deque[float] = field(default_factory=deque)
    gaps: deque[float] = field(default_factory=deque)
    last_event_at: float = 0.0
    _vol: float = 0.0
    _buys: int = 0

    def add_trade(self, t: float, sol: float, side: int) -> None:
        self.trades.append((t, sol, side))
        self._vol += sol
        self._buys += side > 0

    def trim(self, now: float) -> None:
        cut = now - self.window_s
        q = self.trades
        while q and q[0][0] < cut:
            _, sol, side = q.popleft()
            self._vol -= sol
            self._buys -= side > 0
        for d in (self.launches, self.graduations, self.gaps):
            while d and d[0] < cut:
                d.popleft()

    def staleness(self, now: float) -> float:
        return (now - self.last_event_at) if self.last_event_at else float("inf")

    def stats(self, now: float) -> dict:
        self.trim(now)
        minutes = self.window_s / 60
        n = len(self.trades)
        return {
            "trades_per_min": n / minutes,
            "launches_per_min": len(self.launches) / minutes,
            "graduations_window": len(self.graduations),
            "sol_volume_per_min": max(self._vol, 0.0) / minutes,
            "buy_ratio": self._buys / n if n else 0.0,
            "gaps_window": len(self.gaps),
            "data_staleness_s": self.staleness(now),
        }


class MarketDataEngine:
    def __init__(
        self,
        cfg: AppConfig,
        clock: Clock,
        schedule: FeeSchedule,
        registry: TokenRegistry | None = None,
        pool_to_mint: dict[str, str] | None = None,
    ) -> None:
        self.cfg, self.clock, self.schedule = cfg, clock, schedule
        self.registry = registry or TokenRegistry()
        self.tokens: dict[str, TokenMarket] = {}
        self.pool_to_mint = pool_to_mint if pool_to_mint is not None else {}
        self.activity = GlobalActivity(window_s=cfg.regime.window_s)
        self.listeners: list[Callable[[MarketEvent, TokenMarket | None], None]] = []
        self.events_processed = 0
        self.unresolved_pool_events = 0

    # ---------------------------------------------------------------------------------------------------
    def market(self, mint: str) -> TokenMarket | None:
        return self.tokens.get(mint)

    def _ensure(self, ev: MarketEvent, from_create: bool) -> TokenMarket:
        assert ev.mint
        rec, new = self.registry.ensure(ev.mint, ev.t, ev.slot, from_create=from_create)
        tm = self.tokens.get(ev.mint)
        if tm is None:
            tm = TokenMarket(record=rec, series=TradeSeries(self.cfg.tracking.series_retention_s))
            self.tokens[ev.mint] = tm
            if new and not from_create:
                METRICS.inc("discovery.partial_history_tokens")
        return tm

    def on_event(self, ev: MarketEvent) -> TokenMarket | None:
        self.events_processed += 1
        self.activity.last_event_at = max(self.activity.last_event_at, ev.t)
        tm: TokenMarket | None = None
        k = ev.kind
        if k is EventKind.DATA_GAP:
            self.activity.gaps.append(ev.t)
            for m in self.tokens.values():
                if m.record.state not in (S.DEAD, S.EXITED, S.UNTRADEABLE):
                    m.last_gap_at = ev.t
            log.warning("data_gap", slot=ev.slot, reason=ev.extra.get("reason"))
        elif k is EventKind.CREATE:
            tm = self._on_create(ev)
        elif k is EventKind.TRADE:
            tm = self._on_curve_trade(ev)
        elif k is EventKind.COMPLETE:
            tm = self._on_complete(ev)
        elif k in (EventKind.MIGRATE, EventKind.POOL_CREATE):
            tm = self._on_pool(ev)
        elif k is EventKind.AMM_TRADE:
            tm = self._on_amm_trade(ev)
        elif k is EventKind.HOLDER_SNAPSHOT and ev.mint in self.tokens:
            tm = self.tokens[ev.mint]
            tm.holder_snapshot = HolderSnapshot(
                ev.t, list(ev.extra.get("top_balances", [])), int(ev.extra.get("supply", 0))
            )
        if tm is not None:
            tm.record.last_seen_slot = max(tm.record.last_seen_slot, ev.slot)
            tm.record.last_seen_at = ev.t
        for fn in self.listeners:
            fn(ev, tm)
        return tm

    # ---------------------------------------------------------------------------------------------------
    def _on_create(self, ev: MarketEvent) -> TokenMarket:
        tm = self._ensure(ev, from_create=True)
        rec = tm.record
        rec.partial_history = False
        rec.name, rec.symbol, rec.uri = ev.name, ev.symbol, ev.uri
        rec.creator, rec.created_at, rec.created_slot = ev.creator, ev.chain_time or ev.t, ev.slot
        rec.created_at = ev.t  # observation time keeps all ages on one clock
        rec.token_program, rec.quote_mint, rec.is_mayhem = ev.token_program, ev.quote_mint, ev.is_mayhem
        rec.bonding_curve_address = ev.extra.get("bonding_curve")
        pc = self.cfg.pump
        tm.curve = BondingCurveState(
            virtual_token_reserves=int(ev.virtual_token_reserves or pc.initial_virtual_token_reserves),
            virtual_quote_reserves=int(ev.virtual_quote_reserves or pc.initial_virtual_sol_reserves),
            real_token_reserves=int(ev.real_token_reserves or pc.initial_real_token_reserves),
            real_quote_reserves=0,
            token_total_supply=int(ev.extra.get("token_total_supply") or pc.token_total_supply),
            creator=ev.creator,
            is_mayhem_mode=ev.is_mayhem,
            creator_fee_bps_override=int(ev.extra.get("creator_fee_bps") or 0),
            initial_real_token_reserves=pc.initial_real_token_reserves,
        )
        if ev.creator:
            self.registry.register_creator(ev.creator, rec.mint)
        self.activity.launches.append(ev.t)
        self.registry.try_transition(rec, S.BONDING_CURVE, ev.t, "create_event")
        if ev.is_mayhem and not pc.allow_mayhem_mode:
            self.registry.mark_untradeable(rec, ev.t, "mayhem_mode")
        elif ev.quote_mint and ev.quote_mint != _WSOL and not pc.allow_non_sol_quote:
            self.registry.mark_untradeable(rec, ev.t, "non_sol_quote")
        METRICS.inc("discovery.tokens_discovered")
        return tm

    def _record_trade(self, tm: TokenMarket, ev: MarketEvent, price: float, liquidity: int) -> None:
        side = 1 if ev.side is Side.BUY else -1
        tm.series.append(
            Trade(
                t=ev.t,
                slot=ev.slot,
                side=side,
                sol=ev.sol_amount,
                tokens=ev.token_amount,
                user=ev.user or "",
                price=price,
                liquidity=liquidity,
                venue=ev.venue.value if ev.venue else "",
            )
        )
        if ev.user:
            tm.apply_balance(ev.user, side * ev.token_amount, ev.t)
        rec = tm.record
        rec.last_trade_at = ev.t
        tm.peak_liquidity = max(tm.peak_liquidity, liquidity)
        if ev.user and rec.creator and ev.user == rec.creator:
            if side > 0:
                tm.creator_bought += ev.token_amount
            else:
                tm.creator_sold += ev.token_amount
                if tm.creator_first_sell_at is None:
                    tm.creator_first_sell_at = ev.t
        elif side > 0 and rec.created_slot is not None and ev.user:
            if ev.slot == rec.created_slot:
                tm.launch_slot_buyers.add(ev.user)
            if ev.slot - rec.created_slot <= 2:
                tm.sniper_tokens += ev.token_amount
        self.activity.add_trade(ev.t, ev.sol_amount / LAMPORTS_PER_SOL, side)
        if rec.state is S.DEAD:
            self.registry.try_transition(
                rec, S.PUMPSWAP if ev.venue is Venue.PUMPSWAP else S.BONDING_CURVE, ev.t, "revived"
            )

    def _on_curve_trade(self, ev: MarketEvent) -> TokenMarket:
        tm = self._ensure(ev, from_create=False)
        rec = tm.record
        if rec.state is S.NEW or rec.state is S.EXITED:
            self.registry.try_transition(rec, S.BONDING_CURVE, ev.t, "trade_observed")
            if rec.creator is None and ev.creator:
                rec.creator = ev.creator
        prev = tm.curve
        vt, vq = ev.virtual_token_reserves, ev.virtual_quote_reserves
        if vt is None or vq is None:
            METRICS.inc("ingest.trade_without_reserves")
            return tm
        if prev is not None:
            expected_vt = prev.virtual_token_reserves - ev.token_amount * (1 if ev.side is Side.BUY else -1)
            if expected_vt != vt:
                tm.reserve_mismatches += 1
                tm.last_gap_at = ev.t
                METRICS.inc("ingest.reserve_mismatch")
        pc = self.cfg.pump
        tm.curve = BondingCurveState(
            virtual_token_reserves=int(vt),
            virtual_quote_reserves=int(vq),
            real_token_reserves=int(ev.real_token_reserves or 0),
            real_quote_reserves=int(ev.real_quote_reserves or 0),
            token_total_supply=prev.token_total_supply if prev else pc.token_total_supply,
            complete=(ev.real_token_reserves == 0),
            creator=ev.creator or (prev.creator if prev else rec.creator),
            is_mayhem_mode=ev.is_mayhem,
            creator_fee_bps_override=prev.creator_fee_bps_override if prev else 0,
            initial_real_token_reserves=pc.initial_real_token_reserves,
        )
        self._record_trade(tm, ev, tm.curve.spot_price if vt else 0.0, tm.curve.real_quote_reserves)
        if tm.curve.complete:
            self._mark_graduating(tm, ev.t, "curve_exhausted")
        return tm

    def _mark_graduating(self, tm: TokenMarket, t: float, reason: str) -> None:
        rec = tm.record
        rec.graduation_state = "COMPLETE"
        if rec.state in (S.BONDING_CURVE, S.NEW, S.DEAD):
            self.registry.try_transition(rec, S.GRADUATING, t, reason)
            self.activity.graduations.append(t)

    def _on_complete(self, ev: MarketEvent) -> TokenMarket:
        tm = self._ensure(ev, from_create=False)
        if tm.curve is not None:
            from dataclasses import replace

            tm.curve = replace(tm.curve, complete=True)
        self._mark_graduating(tm, ev.t, "complete_event")
        return tm

    def _on_pool(self, ev: MarketEvent) -> TokenMarket:
        tm = self._ensure(ev, from_create=False)
        rec = tm.record
        if ev.pool:
            rec.pool_address = ev.pool
            self.pool_to_mint[ev.pool] = rec.mint
        rec.graduation_state = "MIGRATED"
        if ev.kind is EventKind.POOL_CREATE and ev.pool_base_reserves:
            pool_creator = ev.extra.get("pool_creator")
            from solders.pubkey import Pubkey

            from tradingagent.pump.constants import is_canonical_pump_pool

            canonical = True
            if pool_creator:
                try:
                    canonical = is_canonical_pump_pool(Pubkey.from_string(rec.mint), Pubkey.from_string(pool_creator))
                except ValueError:
                    canonical = False
            tm.pool = PoolState(
                base_reserve=int(ev.pool_base_reserves),
                quote_reserve=int(ev.pool_quote_reserves or 0),
                virtual_quote_reserves=int(ev.pool_virtual_quote_reserves or 0),
                has_coin_creator=bool(ev.creator and ev.creator != "11111111111111111111111111111111"),
                is_canonical=canonical,
                is_mayhem_mode=ev.is_mayhem,
            )
        if rec.state in (S.GRADUATING, S.BONDING_CURVE, S.NEW, S.DEAD, S.EXITED):
            self.registry.try_transition(rec, S.PUMPSWAP, ev.t, ev.kind.value.lower())
        return tm

    def _on_amm_trade(self, ev: MarketEvent) -> TokenMarket | None:
        if ev.mint is None and ev.pool:
            ev.mint = self.pool_to_mint.get(ev.pool)
        if ev.mint is None:
            self.unresolved_pool_events += 1
            METRICS.inc("ingest.unresolved_pool_event")
            return None
        tm = self._ensure(ev, from_create=False)
        rec = tm.record
        if ev.pool and not rec.pool_address:
            rec.pool_address = ev.pool
        if rec.state in (S.NEW, S.GRADUATING, S.BONDING_CURVE, S.EXITED):
            self.registry.try_transition(rec, S.PUMPSWAP, ev.t, "amm_trade_observed")
        prev = tm.pool
        base, quote = ev.pool_base_reserves, ev.pool_quote_reserves
        if base is None or quote is None:
            return tm
        if prev is not None and "pre_base" in ev.extra and ev.extra["pre_base"] != prev.base_reserve:
            tm.reserve_mismatches += 1
            tm.last_gap_at = ev.t
            METRICS.inc("ingest.reserve_mismatch")
        tm.pool = PoolState(
            base_reserve=int(base),
            quote_reserve=int(quote),
            virtual_quote_reserves=int(ev.pool_virtual_quote_reserves or 0),
            has_coin_creator=prev.has_coin_creator if prev else True,
            is_canonical=prev.is_canonical if prev else True,
            is_mayhem_mode=prev.is_mayhem_mode if prev else False,
        )
        tm.pool_trades += 1
        self._record_trade(tm, ev, tm.pool.spot_price, tm.pool.effective_quote)
        if rec.state is S.PUMPSWAP and tm.pool_trades >= self.cfg.tracking.active_after_pool_trades:
            self.registry.try_transition(rec, S.ACTIVE, ev.t, "pool_history_sufficient")
        return tm

    # ---------------------------------------------------------------------------------------------------
    def sweep(self, now: float, protected: set[str] | None = None) -> list[TokenRecord]:
        """Lifecycle housekeeping: DEAD detection and eviction. `protected` mints (open positions) never evict."""
        protected = protected or set()
        tc = self.cfg.tracking
        evicted: list[TokenRecord] = []
        for mint, tm in list(self.tokens.items()):
            rec = tm.record
            idle = now - (rec.last_trade_at or rec.first_seen_at)
            if rec.state in (S.BONDING_CURVE, S.PUMPSWAP, S.ACTIVE, S.NEW) and idle > tc.dead_after_idle_s:
                self.registry.try_transition(rec, S.DEAD, now, f"idle_{int(idle)}s")
            elif (
                rec.state in (S.BONDING_CURVE, S.PUMPSWAP, S.ACTIVE)
                and idle > 120
                and tm.liquidity_lamports() < tc.dead_below_liquidity_sol * LAMPORTS_PER_SOL
                and tm.peak_liquidity > 4 * tc.dead_below_liquidity_sol * LAMPORTS_PER_SOL
            ):
                self.registry.try_transition(rec, S.DEAD, now, "liquidity_collapsed")
            if mint not in protected and idle > tc.evict_after_idle_s:
                self.registry.try_transition(rec, S.EXITED, now, "evicted")
                del self.tokens[mint]
                self.registry.remove(mint)
                evicted.append(rec)
        if len(self.tokens) > tc.max_tracked_tokens:
            by_idle = sorted(self.tokens.values(), key=lambda m: m.record.last_trade_at or m.record.first_seen_at)
            for tm in by_idle[: len(self.tokens) - tc.max_tracked_tokens]:
                if tm.mint in protected:
                    continue
                self.registry.try_transition(tm.record, S.EXITED, now, "capacity")
                del self.tokens[tm.mint]
                self.registry.remove(tm.mint)
                evicted.append(tm.record)
        METRICS.set("market.tracked_tokens", len(self.tokens))
        return evicted

    def snapshot(self, tm: TokenMarket, now: float) -> dict:
        w = tm.series.window(now, 300.0)
        px = tm.spot_price()
        mcap = tm.market_cap_lamports()
        return {
            "mint": tm.mint,
            "t": now,
            "state": tm.record.state.value,
            "venue": tm.venue.value if tm.venue else None,
            "price_lamports_per_token": px,
            "market_cap_sol": (mcap / LAMPORTS_PER_SOL) if mcap else None,
            "liquidity_sol": tm.liquidity_lamports() / LAMPORTS_PER_SOL,
            "volume_sol_5m": sum(x.sol for x in w) / LAMPORTS_PER_SOL,
            "buy_volume_sol_5m": sum(x.sol for x in w if x.side > 0) / LAMPORTS_PER_SOL,
            "buys_5m": sum(1 for x in w if x.side > 0),
            "sells_5m": sum(1 for x in w if x.side < 0),
            "holders": len(tm.holders),
            "progress": tm.curve.progress if tm.curve and tm.venue is Venue.BONDING_CURVE else None,
            "virtual_token_reserves": tm.curve.virtual_token_reserves if tm.curve else None,
            "virtual_quote_reserves": tm.curve.virtual_quote_reserves if tm.curve else None,
            "real_token_reserves": tm.curve.real_token_reserves if tm.curve else None,
            "real_quote_reserves": tm.curve.real_quote_reserves if tm.curve else None,
            "pool_base_reserves": tm.pool.base_reserve if tm.pool else None,
            "pool_quote_reserves": tm.pool.quote_reserve if tm.pool else None,
        }

    def token_program_is_2022(self, tm: TokenMarket) -> bool:
        return tm.record.token_program == _TOKEN_2022
