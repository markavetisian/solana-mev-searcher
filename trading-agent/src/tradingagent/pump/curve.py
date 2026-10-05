"""Exact integer bonding-curve math for the Pump program.

Mirrors @pump-fun/pump-sdk 2.0.0 (bondingCurve.ts / fees.ts) lamport-for-lamport:

  buy  (exact quote in): input  = (spend - 1) * 10_000 // (10_000 + total_fee_bps)
                         tokens = input * vT // (vQ + input), capped at real token reserves
  buy  (exact tokens):   cost   = tokens * vQ // (vT - tokens) + 1 ; fee = ceil(cost * bps / 1e4) per fee leg
  sell (exact tokens):   gross  = tokens * vQ // (vT + tokens)      ; net = gross - fees

All amounts are integers in base units (lamports, token base units with 6 decimals).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from tradingagent.common.types import Side, Venue
from tradingagent.common.units import PUMP_TOTAL_SUPPLY
from tradingagent.pump.fees import Fees, FeeSchedule, fee_amount

DEFAULT_PUBKEY_STR = "11111111111111111111111111111111"


class CurveError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BondingCurveState:
    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    real_quote_reserves: int
    token_total_supply: int = PUMP_TOTAL_SUPPLY
    complete: bool = False
    creator: str | None = None
    is_mayhem_mode: bool = False
    creator_fee_bps_override: int = 0
    creator_fee_configurable: bool = False
    initial_real_token_reserves: int = 793_100_000_000_000

    @property
    def has_creator(self) -> bool:
        return bool(self.creator) and self.creator != DEFAULT_PUBKEY_STR

    @property
    def spot_price(self) -> float:
        """Marginal price in lamports per token base unit (no fees)."""
        if self.virtual_token_reserves <= 0:
            raise CurveError("migrated / empty curve")
        return self.virtual_quote_reserves / self.virtual_token_reserves

    @property
    def market_cap_lamports(self) -> int:
        supply = self.token_total_supply if self.is_mayhem_mode else PUMP_TOTAL_SUPPLY
        if self.virtual_token_reserves <= 0:
            raise CurveError("migrated / empty curve")
        return self.virtual_quote_reserves * supply // self.virtual_token_reserves

    @property
    def progress(self) -> float:
        """Fraction of sellable curve tokens already bought (1.0 = curve complete)."""
        if self.initial_real_token_reserves <= 0:
            return 0.0
        return max(0.0, min(1.0, 1 - self.real_token_reserves / self.initial_real_token_reserves))

    def fees(self, schedule: FeeSchedule) -> Fees:
        f = schedule.tier_for(self.market_cap_lamports)
        if self.creator_fee_configurable and self.creator_fee_bps_override > 0:
            f = Fees(f.lp_bps, f.protocol_bps, self.creator_fee_bps_override)
        return f

    def _fee_legs(self, amount: int, f: Fees) -> tuple[int, int]:
        protocol = fee_amount(amount, f.protocol_bps)
        creator = fee_amount(amount, f.creator_bps) if self.has_creator else 0
        return protocol, creator


@dataclass(frozen=True, slots=True)
class TradeQuote:
    venue: Venue
    side: Side
    amount_in: int  # lamports for buys (incl. fees), token base units for sells
    amount_out: int  # token base units for buys, lamports for sells (net of fees)
    quote_amount_gross: int  # curve/pool quote amount excluding fees
    protocol_fee: int
    creator_fee: int
    lp_fee: int
    spot_price_before: float  # lamports per base unit
    state_after: object

    @property
    def total_fee(self) -> int:
        return self.protocol_fee + self.creator_fee + self.lp_fee

    @property
    def token_amount(self) -> int:
        return self.amount_out if self.side is Side.BUY else self.amount_in

    @property
    def effective_price(self) -> float:
        """All-in lamports per base unit (buys: paid incl. fees; sells: received net of fees)."""
        tokens = self.token_amount
        if tokens <= 0:
            return float("nan")
        quote = self.amount_in if self.side is Side.BUY else self.amount_out
        return quote / tokens

    @property
    def price_impact(self) -> float:
        """Fee-free execution price vs pre-trade spot (positive = worse for us)."""
        tokens = self.token_amount
        if tokens <= 0 or self.spot_price_before <= 0:
            return float("nan")
        px = self.quote_amount_gross / tokens
        return px / self.spot_price_before - 1 if self.side is Side.BUY else 1 - px / self.spot_price_before

    @property
    def all_in_slippage(self) -> float:
        """Effective (fee-inclusive) price vs pre-trade spot (positive = worse for us)."""
        eff = self.effective_price
        if self.side is Side.BUY:
            return eff / self.spot_price_before - 1
        return 1 - eff / self.spot_price_before


def quote_buy_exact_quote_in(state: BondingCurveState, spend_lamports: int, schedule: FeeSchedule) -> TradeQuote:
    """Tokens received for spending at most `spend_lamports` (fees included), per buy_exact_quote_in_v2."""
    if state.complete or state.virtual_token_reserves <= 0:
        raise CurveError("curve complete: not tradeable on the bonding curve")
    if spend_lamports <= 1:
        raise CurveError("spend too small")
    f = state.fees(schedule)
    total_bps = f.protocol_bps + (f.creator_bps if state.has_creator else 0)
    input_amount = (spend_lamports - 1) * 10_000 // (total_bps + 10_000)
    tokens = input_amount * state.virtual_token_reserves // (state.virtual_quote_reserves + input_amount)
    tokens = min(tokens, state.real_token_reserves)
    if tokens <= 0:
        raise CurveError("zero tokens out")
    return quote_buy_exact_tokens(state, tokens, schedule)


def quote_buy_exact_tokens(state: BondingCurveState, tokens: int, schedule: FeeSchedule) -> TradeQuote:
    if state.complete or state.virtual_token_reserves <= 0:
        raise CurveError("curve complete: not tradeable on the bonding curve")
    tokens = min(tokens, state.real_token_reserves)
    if tokens <= 0:
        raise CurveError("zero tokens")
    cost = tokens * state.virtual_quote_reserves // (state.virtual_token_reserves - tokens) + 1
    f = state.fees(schedule)
    protocol, creator = state._fee_legs(cost, f)
    real_token_after = state.real_token_reserves - tokens
    after = replace(
        state,
        virtual_token_reserves=state.virtual_token_reserves - tokens,
        virtual_quote_reserves=state.virtual_quote_reserves + cost,
        real_token_reserves=real_token_after,
        real_quote_reserves=state.real_quote_reserves + cost,
        complete=real_token_after == 0,
    )
    return TradeQuote(
        venue=Venue.BONDING_CURVE,
        side=Side.BUY,
        amount_in=cost + protocol + creator,
        amount_out=tokens,
        quote_amount_gross=cost,
        protocol_fee=protocol,
        creator_fee=creator,
        lp_fee=0,
        spot_price_before=state.spot_price,
        state_after=after,
    )


def quote_sell(state: BondingCurveState, tokens: int, schedule: FeeSchedule) -> TradeQuote:
    if state.complete or state.virtual_token_reserves <= 0:
        raise CurveError("curve complete: not tradeable on the bonding curve")
    if tokens <= 0:
        raise CurveError("zero tokens")
    gross = tokens * state.virtual_quote_reserves // (state.virtual_token_reserves + tokens)
    if gross > state.real_quote_reserves:
        raise CurveError("insufficient real quote reserves")
    f = state.fees(schedule)
    protocol, creator = state._fee_legs(gross, f)
    net = gross - protocol - creator
    after = replace(
        state,
        virtual_token_reserves=state.virtual_token_reserves + tokens,
        virtual_quote_reserves=state.virtual_quote_reserves - gross,
        real_token_reserves=state.real_token_reserves + tokens,
        real_quote_reserves=state.real_quote_reserves - gross,
    )
    return TradeQuote(
        venue=Venue.BONDING_CURVE,
        side=Side.SELL,
        amount_in=tokens,
        amount_out=max(net, 0),
        quote_amount_gross=gross,
        protocol_fee=protocol,
        creator_fee=creator,
        lp_fee=0,
        spot_price_before=state.spot_price,
        state_after=after,
    )


def initial_state(
    initial_virtual_token: int = 1_073_000_000_000_000,
    initial_virtual_quote: int = 30_000_000_000,
    initial_real_token: int = 793_100_000_000_000,
    total_supply: int = PUMP_TOTAL_SUPPLY,
    creator: str | None = None,
) -> BondingCurveState:
    return BondingCurveState(
        virtual_token_reserves=initial_virtual_token,
        virtual_quote_reserves=initial_virtual_quote,
        real_token_reserves=initial_real_token,
        real_quote_reserves=0,
        token_total_supply=total_supply,
        creator=creator,
        initial_real_token_reserves=initial_real_token,
    )


def state_from_account(acct: dict, initial_real_token: int = 793_100_000_000_000) -> BondingCurveState:
    """From a decoded BondingCurve account (field names per the current IDL; older names accepted)."""
    vq = acct.get("virtual_quote_reserves", acct.get("virtual_sol_reserves"))
    rq = acct.get("real_quote_reserves", acct.get("real_sol_reserves"))
    return BondingCurveState(
        virtual_token_reserves=int(acct["virtual_token_reserves"]),
        virtual_quote_reserves=int(vq),
        real_token_reserves=int(acct["real_token_reserves"]),
        real_quote_reserves=int(rq),
        token_total_supply=int(acct.get("token_total_supply") or PUMP_TOTAL_SUPPLY),
        complete=bool(acct.get("complete", False)),
        creator=acct.get("creator"),
        is_mayhem_mode=bool(acct.get("is_mayhem_mode", False)),
        creator_fee_bps_override=int(acct.get("creator_fee_bps") or 0),
        initial_real_token_reserves=initial_real_token,
    )
