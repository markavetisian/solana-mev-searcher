"""Execution simulator: what a trade will actually cost BEFORE it is sent.

For a buy of `spend` lamports on the current venue state it computes the exact on-chain quote (fees, impact),
then simulates the full exit of the acquired tokens against the post-entry state (our own impact included), and
adds network costs (base fee, priority fee) and an execution-risk term for latency (price moves between decision
and landing). Rent for a new token account is tracked separately: it is refunded when the account is closed on
exit, so it is capital, not cost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from tradingagent.common.config import ExecutionConfig
from tradingagent.common.types import Side, Venue
from tradingagent.common.units import (
    BASE_FEE_LAMPORTS_PER_SIGNATURE,
    LAMPORTS_PER_SOL,
    SPL_TOKEN_ACCOUNT_RENT_LAMPORTS,
    TOKEN2022_ACCOUNT_RENT_LAMPORTS,
)
from tradingagent.pump.adapters import MarketAdapter, QuoteError


@dataclass
class ExecutionEstimate:
    ok: bool
    venue: Venue | None
    side: Side
    reasons: list[str] = field(default_factory=list)
    expected_input: int = 0
    expected_output: int = 0
    minimum_output: int = 0
    quoted_price: float = 0.0  # spot before (lamports / base unit)
    expected_price: float = 0.0  # all-in effective price
    price_impact: float = 0.0
    expected_slippage: float = 0.0  # all-in vs spot
    platform_fees: int = 0
    base_fee: int = 0
    priority_fee: int = 0
    rent_deposit: int = 0
    total_expected_cost: int = 0  # entry: fees + impact + network, in lamports
    exit_expected_output: int = 0
    exit_cost: int = 0  # lamports lost on exit vs spot (fees + impact) + exit network fees
    exit_slippage: float = 0.0
    roundtrip_cost_frac: float = 0.0  # 1 - exit proceeds / entry spend (fees + both impacts)
    network_cost_frac: float = 0.0
    execution_risk_frac: float = 0.0
    liquidity_lamports: int = 0

    @property
    def total_cost_frac(self) -> float:
        return self.roundtrip_cost_frac + self.network_cost_frac + self.execution_risk_frac

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["venue"] = self.venue.value if self.venue else None
        d["side"] = self.side.value
        d["total_cost_frac"] = self.total_cost_frac
        return d


class ExecutionSimulator:
    def __init__(self, cfg: ExecutionConfig) -> None:
        self.cfg = cfg

    def priority_fee_lamports(self, micro_lamports_per_cu: int) -> int:
        fee = self.cfg.compute_unit_limit * micro_lamports_per_cu // 1_000_000
        return min(fee, self.cfg.priority_fee.max_priority_fee_lamports)

    def simulate_entry(
        self,
        adapter: MarketAdapter,
        spend_lamports: int,
        priority_micro_lamports: int,
        volatility_5s: float | None,
        needs_token_account: bool = True,
        token_2022: bool = False,
    ) -> ExecutionEstimate:
        est = ExecutionEstimate(ok=False, venue=adapter.venue, side=Side.BUY, expected_input=spend_lamports)
        try:
            buy = adapter.quote_buy(spend_lamports)
            sell = adapter.after(buy).quote_sell(buy.amount_out)
        except QuoteError as e:
            est.reasons.append(f"quote_failed: {e}")
            return est
        slip = self.cfg.max_slippage_bps / 10_000
        prio = self.priority_fee_lamports(priority_micro_lamports)
        net_fee = BASE_FEE_LAMPORTS_PER_SIGNATURE + prio
        est.liquidity_lamports = adapter.liquidity_lamports()
        est.expected_output = buy.amount_out
        est.minimum_output = int(buy.amount_out * (1 - slip))
        est.quoted_price = buy.spot_price_before
        est.expected_price = buy.effective_price
        est.price_impact = buy.price_impact
        est.expected_slippage = buy.all_in_slippage
        est.platform_fees = buy.total_fee
        est.base_fee = BASE_FEE_LAMPORTS_PER_SIGNATURE
        est.priority_fee = prio
        if needs_token_account:
            est.rent_deposit = TOKEN2022_ACCOUNT_RENT_LAMPORTS if token_2022 else SPL_TOKEN_ACCOUNT_RENT_LAMPORTS
        spot_value = buy.amount_out * buy.spot_price_before
        est.total_expected_cost = int(spend_lamports - spot_value) + net_fee
        est.exit_expected_output = sell.amount_out
        est.exit_slippage = sell.all_in_slippage
        est.exit_cost = int(buy.amount_out * sell.spot_price_before - sell.amount_out) + net_fee
        est.roundtrip_cost_frac = 1 - sell.amount_out / spend_lamports
        est.network_cost_frac = 2 * net_fee / spend_lamports
        # Latency risk: the price can drift against us between decision and landing.
        lat = max(self.cfg.assumed_latency_s, 0.0)
        vol = volatility_5s if volatility_5s is not None else 0.05
        est.execution_risk_frac = 0.5 * vol * math.sqrt(lat / 5.0) + self.cfg.paper_extra_adverse_bps / 10_000
        est.ok = True
        return est

    def simulate_exit(self, adapter: MarketAdapter, tokens: int, priority_micro_lamports: int) -> ExecutionEstimate:
        est = ExecutionEstimate(ok=False, venue=adapter.venue, side=Side.SELL, expected_input=tokens)
        try:
            sell = adapter.quote_sell(tokens)
        except QuoteError as e:
            est.reasons.append(f"quote_failed: {e}")
            return est
        slip = self.cfg.max_slippage_bps / 10_000
        prio = self.priority_fee_lamports(priority_micro_lamports)
        est.expected_output = sell.amount_out
        est.minimum_output = int(sell.amount_out * (1 - slip))
        est.quoted_price = sell.spot_price_before
        est.expected_price = sell.effective_price
        est.price_impact = sell.price_impact
        est.expected_slippage = sell.all_in_slippage
        est.platform_fees = sell.total_fee
        est.base_fee = BASE_FEE_LAMPORTS_PER_SIGNATURE
        est.priority_fee = prio
        est.exit_expected_output = sell.amount_out
        est.exit_slippage = sell.all_in_slippage
        est.total_expected_cost = (
            int(tokens * sell.spot_price_before - sell.amount_out) + BASE_FEE_LAMPORTS_PER_SIGNATURE + prio
        )
        est.liquidity_lamports = adapter.liquidity_lamports()
        est.ok = True
        return est

    def check_limits(self, est: ExecutionEstimate, max_entry_impact: float, max_exit_slippage: float) -> list[str]:
        out: list[str] = []
        if not est.ok:
            return est.reasons or ["simulation_failed"]
        if est.price_impact > max_entry_impact:
            out.append(f"entry price impact {est.price_impact:.2%} > {max_entry_impact:.2%}")
        if est.exit_slippage > max_exit_slippage:
            out.append(f"expected exit slippage {est.exit_slippage:.2%} > {max_exit_slippage:.2%}")
        if est.total_cost_frac > self.cfg.max_total_cost_frac:
            out.append(f"total execution cost {est.total_cost_frac:.2%} > {self.cfg.max_total_cost_frac:.2%}")
        if est.priority_fee > self.cfg.priority_fee.max_priority_fee_lamports:
            out.append("priority fee above cap")
        return out


def lamports_fmt(x: int) -> str:
    return f"{x / LAMPORTS_PER_SOL:.6f} SOL"
