"""Per-token market state: venue state, trade series, holder ledger, creator tracking."""

from __future__ import annotations

from dataclasses import dataclass, field

from tradingagent.common.types import LifecycleState, Venue
from tradingagent.common.units import PUMP_TOTAL_SUPPLY
from tradingagent.discovery.registry import TokenRecord
from tradingagent.market.series import TradeSeries
from tradingagent.pump.adapters import BondingCurveAdapter, MarketAdapter, PumpSwapAdapter
from tradingagent.pump.amm import PoolState
from tradingagent.pump.curve import BondingCurveState
from tradingagent.pump.fees import FeeSchedule


@dataclass
class HolderSnapshot:
    """RPC enrichment (getTokenLargestAccounts) recorded as an event so replays see identical data."""

    t: float
    top_balances: list[int]  # largest token accounts, excluding curve/pool vaults
    supply: int


@dataclass
class TokenMarket:
    record: TokenRecord
    series: TradeSeries
    curve: BondingCurveState | None = None
    pool: PoolState | None = None
    holders: dict[str, int] = field(default_factory=dict)
    first_buy_at: dict[str, float] = field(default_factory=dict)
    holder_count_hist: list[tuple[float, int]] = field(default_factory=list)
    early_buyers: list[str] = field(default_factory=list)  # first 20 distinct buyers
    creator_bought: int = 0
    creator_sold: int = 0
    creator_first_sell_at: float | None = None
    launch_slot_buyers: set[str] = field(default_factory=set)
    sniper_tokens: int = 0  # bought by non-creators within 2 slots of creation
    pool_trades: int = 0
    last_gap_at: float | None = None
    reserve_mismatches: int = 0
    holder_snapshot: HolderSnapshot | None = None
    peak_liquidity: int = 0

    @property
    def mint(self) -> str:
        return self.record.mint

    @property
    def venue(self) -> Venue | None:
        st = self.record.state
        if st in (LifecycleState.PUMPSWAP, LifecycleState.ACTIVE) and self.pool is not None:
            return Venue.PUMPSWAP
        if (
            st in (LifecycleState.BONDING_CURVE, LifecycleState.NEW)
            and self.curve is not None
            and not self.curve.complete
        ):
            return Venue.BONDING_CURVE
        if st is LifecycleState.DEAD:
            if self.pool is not None:
                return Venue.PUMPSWAP
            if self.curve is not None and not self.curve.complete:
                return Venue.BONDING_CURVE
        return None

    def adapter(self, schedule: FeeSchedule) -> MarketAdapter | None:
        v = self.venue
        if v is Venue.PUMPSWAP and self.pool is not None and self.pool.base_reserve > 0:
            return PumpSwapAdapter(self.pool, schedule)
        if v is Venue.BONDING_CURVE and self.curve is not None and self.curve.virtual_token_reserves > 0:
            return BondingCurveAdapter(self.curve, schedule)
        return None

    def spot_price(self) -> float | None:
        try:
            if self.venue is Venue.PUMPSWAP and self.pool:
                return self.pool.spot_price
            if self.curve:
                return self.curve.spot_price
        except (ValueError, ZeroDivisionError):
            return None
        return None

    def liquidity_lamports(self) -> int:
        if self.venue is Venue.PUMPSWAP and self.pool:
            return self.pool.effective_quote
        if self.curve:
            return self.curve.real_quote_reserves
        return 0

    def market_cap_lamports(self) -> int | None:
        px = self.spot_price()
        return int(px * PUMP_TOTAL_SUPPLY) if px else None

    @property
    def creator_balance(self) -> int:
        c = self.record.creator
        return self.holders.get(c, 0) if c else 0

    def apply_balance(self, user: str, delta: int, t: float) -> None:
        bal = self.holders.get(user, 0) + delta
        before = len(self.holders)
        if bal > 0:
            self.holders[user] = bal
            if user not in self.first_buy_at:
                self.first_buy_at[user] = t
                if len(self.early_buyers) < 20:
                    self.early_buyers.append(user)
        else:
            self.holders.pop(user, None)
        if len(self.holders) != before:
            self.holder_count_hist.append((t, len(self.holders)))
            if len(self.holder_count_hist) > 4096:
                del self.holder_count_hist[:2048]

    def holder_count_at(self, as_of: float) -> int:
        import bisect

        i = bisect.bisect_right(self.holder_count_hist, (as_of, float("inf")))
        return self.holder_count_hist[i - 1][1] if i else 0
