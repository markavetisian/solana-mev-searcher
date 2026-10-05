"""Venue adapters: one interface over two very different liquidity models.

BondingCurveAdapter  — Pump bonding curve (virtual-reserve constant product, fees by market-cap tier, capped by
                       real token reserves; becomes untradeable the moment the curve completes).
PumpSwapAdapter      — PumpSwap constant-product pool (effective quote reserves, lp+protocol+creator fees).

Strategy and execution code only talk to `MarketAdapter`, and always know which venue they are quoting.
"""

from __future__ import annotations

from typing import Protocol

from tradingagent.common.types import Venue
from tradingagent.pump.amm import AmmError, PoolState, quote_buy_quote_in, quote_sell_base_in
from tradingagent.pump.curve import (
    BondingCurveState,
    CurveError,
    TradeQuote,
    quote_buy_exact_quote_in,
    quote_sell,
)
from tradingagent.pump.fees import FeeSchedule


class QuoteError(ValueError):
    pass


class MarketAdapter(Protocol):
    venue: Venue

    def spot_price(self) -> float: ...
    def liquidity_lamports(self) -> int: ...
    def market_cap_lamports(self) -> int: ...
    def quote_buy(self, spend_lamports: int) -> TradeQuote: ...
    def quote_sell(self, tokens: int) -> TradeQuote: ...
    def after(self, quote: TradeQuote) -> MarketAdapter: ...


class BondingCurveAdapter:
    venue = Venue.BONDING_CURVE

    def __init__(self, state: BondingCurveState, schedule: FeeSchedule) -> None:
        self.state, self.schedule = state, schedule

    def spot_price(self) -> float:
        return self.state.spot_price

    def liquidity_lamports(self) -> int:
        return self.state.real_quote_reserves

    def market_cap_lamports(self) -> int:
        return self.state.market_cap_lamports

    def quote_buy(self, spend_lamports: int) -> TradeQuote:
        try:
            return quote_buy_exact_quote_in(self.state, spend_lamports, self.schedule)
        except CurveError as e:
            raise QuoteError(str(e)) from e

    def quote_sell(self, tokens: int) -> TradeQuote:
        try:
            return quote_sell(self.state, tokens, self.schedule)
        except CurveError as e:
            raise QuoteError(str(e)) from e

    def after(self, quote: TradeQuote) -> BondingCurveAdapter:
        return BondingCurveAdapter(quote.state_after, self.schedule)  # type: ignore[arg-type]


class PumpSwapAdapter:
    venue = Venue.PUMPSWAP

    def __init__(self, pool: PoolState, schedule: FeeSchedule) -> None:
        self.state, self.schedule = pool, schedule

    def spot_price(self) -> float:
        return self.state.spot_price

    def liquidity_lamports(self) -> int:
        return self.state.effective_quote

    def market_cap_lamports(self) -> int:
        return self.state.market_cap_lamports

    def quote_buy(self, spend_lamports: int) -> TradeQuote:
        try:
            return quote_buy_quote_in(self.state, spend_lamports, self.schedule)
        except AmmError as e:
            raise QuoteError(str(e)) from e

    def quote_sell(self, tokens: int) -> TradeQuote:
        try:
            return quote_sell_base_in(self.state, tokens, self.schedule)
        except AmmError as e:
            raise QuoteError(str(e)) from e

    def after(self, quote: TradeQuote) -> PumpSwapAdapter:
        return PumpSwapAdapter(quote.state_after, self.schedule)  # type: ignore[arg-type]
