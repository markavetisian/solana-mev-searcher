"""Exact PumpSwap constant-product math, mirroring @pump-fun/pump-swap-sdk 1.20.0 (buy.ts / sell.ts).

Prices use *effective* quote reserves = vault balance + Pool.virtual_quote_reserves (signed i128, may be
negative since 2026-09-30). Base reserves are the raw base vault balance.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from tradingagent.common.types import Side, Venue
from tradingagent.common.units import PUMP_TOTAL_SUPPLY
from tradingagent.pump.curve import TradeQuote
from tradingagent.pump.fees import Fees, FeeSchedule, ceil_div, fee_amount


class AmmError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PoolState:
    base_reserve: int  # pool base vault amount
    quote_reserve: int  # pool quote vault amount (raw)
    virtual_quote_reserves: int = 0
    base_mint_supply: int = PUMP_TOTAL_SUPPLY
    has_coin_creator: bool = True
    is_canonical: bool = True
    is_mayhem_mode: bool = False
    creator_fee_bps_override: int = 0
    creator_fee_configurable: bool = False

    @property
    def effective_quote(self) -> int:
        return self.quote_reserve + self.virtual_quote_reserves

    @property
    def spot_price(self) -> float:
        if self.base_reserve <= 0:
            raise AmmError("empty pool")
        return self.effective_quote / self.base_reserve

    @property
    def market_cap_lamports(self) -> int:
        supply = PUMP_TOTAL_SUPPLY if self.is_mayhem_mode else self.base_mint_supply
        if self.base_reserve <= 0:
            raise AmmError("empty pool")
        return self.effective_quote * supply // self.base_reserve

    def fees(self, schedule: FeeSchedule) -> Fees:
        f = schedule.tier_for(self.market_cap_lamports) if self.is_canonical else schedule.flat
        if self.creator_fee_configurable and self.creator_fee_bps_override > 0:
            f = Fees(f.lp_bps, f.protocol_bps, self.creator_fee_bps_override)
        return f


def quote_buy_quote_in(pool: PoolState, quote_in: int, schedule: FeeSchedule) -> TradeQuote:
    """Base tokens out for spending `quote_in` lamports including all fees (SDK buyQuoteInput)."""
    if pool.base_reserve <= 0 or pool.quote_reserve <= 0:
        raise AmmError("empty pool")
    f = pool.fees(schedule)
    creator_bps = f.creator_bps if pool.has_coin_creator else 0
    total_bps = f.lp_bps + f.protocol_bps + creator_bps
    effective_quote = quote_in * 10_000 // (10_000 + total_bps)
    lp, proto = fee_amount(effective_quote, f.lp_bps), fee_amount(effective_quote, f.protocol_bps)
    creator = fee_amount(effective_quote, creator_bps) if pool.has_coin_creator else 0
    total_with_fees = effective_quote + lp + proto + creator
    if total_with_fees > quote_in:
        effective_quote -= total_with_fees - quote_in
    input_amount = effective_quote - 1
    if input_amount <= 0:
        raise AmmError("spend too small")
    base_out = pool.base_reserve * input_amount // (pool.effective_quote + input_amount)
    if base_out <= 0:
        raise AmmError("zero base out")
    # The program then executes an exact-base-out buy for `base_out`; price it the same way the SDK does.
    return quote_buy_base_out(pool, base_out, schedule)


def quote_buy_base_out(pool: PoolState, base_out: int, schedule: FeeSchedule) -> TradeQuote:
    if base_out >= pool.base_reserve:
        raise AmmError("cannot buy entire pool")
    quote_in = ceil_div(pool.effective_quote * base_out, pool.base_reserve - base_out)
    f = pool.fees(schedule)
    lp, proto = fee_amount(quote_in, f.lp_bps), fee_amount(quote_in, f.protocol_bps)
    creator = fee_amount(quote_in, f.creator_bps) if pool.has_coin_creator else 0
    after = replace(pool, base_reserve=pool.base_reserve - base_out, quote_reserve=pool.quote_reserve + quote_in + lp)
    return TradeQuote(
        venue=Venue.PUMPSWAP,
        side=Side.BUY,
        amount_in=quote_in + lp + proto + creator,
        amount_out=base_out,
        quote_amount_gross=quote_in,
        protocol_fee=proto,
        creator_fee=creator,
        lp_fee=lp,
        spot_price_before=pool.spot_price,
        state_after=after,
    )


def quote_sell_base_in(pool: PoolState, base_in: int, schedule: FeeSchedule) -> TradeQuote:
    if base_in <= 0:
        raise AmmError("zero base in")
    gross = pool.effective_quote * base_in // (pool.base_reserve + base_in)
    f = pool.fees(schedule)
    lp, proto = fee_amount(gross, f.lp_bps), fee_amount(gross, f.protocol_bps)
    creator = fee_amount(gross, f.creator_bps) if pool.has_coin_creator else 0
    if pool.quote_reserve < gross - lp:
        raise AmmError("insufficient real quote reserves")
    net = gross - lp - proto - creator
    if net < 0:
        raise AmmError("fees exceed output")
    after = replace(pool, base_reserve=pool.base_reserve + base_in, quote_reserve=pool.quote_reserve - (gross - lp))
    return TradeQuote(
        venue=Venue.PUMPSWAP,
        side=Side.SELL,
        amount_in=base_in,
        amount_out=net,
        quote_amount_gross=gross,
        protocol_fee=proto,
        creator_fee=creator,
        lp_fee=lp,
        spot_price_before=pool.spot_price,
        state_after=after,
    )
