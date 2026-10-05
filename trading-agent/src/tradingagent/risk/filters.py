"""Token risk filter: hard, configurable rejection rules evaluated BEFORE scoring.

Each rule reports observed vs limit, so every rejection is explainable:

    REJECTED  Token: ABC  Reason: expected exit slippage = 18.4%  Maximum allowed = 7.0%
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradingagent.common.config import FilterConfig
from tradingagent.common.versions import FILTER_VERSION
from tradingagent.features.engine import FeatureVector


@dataclass(frozen=True)
class Rejection:
    code: str
    message: str
    observed: float | str | None
    limit: float | str | None

    def render(self) -> str:
        def fmt(x: object) -> str:
            return f"{x:.4g}" if isinstance(x, float) else str(x)

        return f"{self.message}: observed={fmt(self.observed)} limit={fmt(self.limit)}"


@dataclass
class FilterResult:
    passed: bool
    rejections: list[Rejection] = field(default_factory=list)
    version: str = FILTER_VERSION

    def to_dict(self) -> dict:
        return {"passed": self.passed, "version": self.version, "rejections": [r.__dict__ for r in self.rejections]}


class TokenRiskFilter:
    def __init__(self, cfg: FilterConfig) -> None:
        self.cfg = cfg

    def evaluate(self, fv: FeatureVector, state: str) -> FilterResult:
        c = self.cfg
        rej: list[Rejection] = []

        def need_max(code: str, msg: str, value: float | None, limit: float, missing_ok: bool = False) -> None:
            if value is None:
                if not missing_ok:
                    rej.append(Rejection(code, f"{msg} unknown", None, limit))
            elif value > limit:
                rej.append(Rejection(code, msg, value, limit))

        def need_min(code: str, msg: str, value: float | None, limit: float, missing_ok: bool = False) -> None:
            if value is None:
                if not missing_ok:
                    rej.append(Rejection(code, f"{msg} unknown", None, limit))
            elif value < limit:
                rej.append(Rejection(code, msg, value, limit))

        if state not in c.allowed_states:
            rej.append(
                Rejection(
                    "lifecycle_incompatible",
                    "lifecycle state incompatible with strategy",
                    state,
                    ",".join(c.allowed_states),
                )
            )
        need_max("stale_data", "market data staleness (s)", fv.get("data_staleness_s"), c.max_data_staleness_s)
        gap = fv.get("since_data_gap_s")
        if gap is not None and gap < 120:
            rej.append(Rejection("unreliable_data", "seconds since data gap / reserve mismatch", gap, 120.0))
        if c.reject_partial_history and fv.get("partial_history") == 1.0:
            rej.append(
                Rejection("partial_history", "token history incomplete (created before observation)", "partial", "full")
            )
        need_min("insufficient_liquidity", "liquidity (SOL)", fv.get("liquidity_sol"), c.min_liquidity_sol)
        need_max("excessive_price_impact", "entry price impact", fv.get("entry_impact_frac"), c.max_entry_price_impact)
        need_max("exit_slippage", "expected exit slippage", fv.get("exit_impact_frac"), c.max_expected_exit_slippage)
        need_max("holder_concentration", "top-10 holder share", fv.get("top10_share"), c.max_top10_concentration)
        need_max("creator_concentration", "creator holding share", fv.get("creator_holding"), c.max_creator_holding)
        need_max(
            "creator_selling",
            "creator sold fraction",
            fv.get("creator_sold_frac"),
            c.reject_if_creator_sold_pct_over,
            missing_ok=True,
        )
        need_min("insufficient_activity", "trades in last 60s", fv.get("n_trades_60s"), float(c.min_trades_60s))
        need_min(
            "insufficient_traders",
            "unique traders in last 5m",
            fv.get("unique_traders_300s"),
            float(c.min_unique_traders_300s),
        )
        need_min("insufficient_volume", "volume last 5m (SOL)", fv.get("vol_sol_300s"), c.min_volume_sol_300s)
        need_max(
            "excessive_volatility",
            "volatility (5s log-ret stdev, 60s)",
            fv.get("volatility_60s"),
            c.max_volatility_60s,
            missing_ok=True,
        )
        need_min("too_young", "token age (s)", fv.get("age_s"), c.min_token_age_s)
        need_max("too_old", "token age (s)", fv.get("age_s"), c.max_token_age_s)
        need_max(
            "bundled_launch",
            "wallets buying in creation slot",
            fv.get("launch_slot_buyers"),
            float(c.max_same_slot_launch_buyers),
            missing_ok=True,
        )
        need_max(
            "sniper_concentration",
            "supply bought by snipers (<=2 slots)",
            fv.get("sniper_supply_share"),
            c.max_sniper_supply_share,
            missing_ok=True,
        )
        prog = fv.get("progress")
        if prog is not None and prog > c.max_bonding_progress:
            rej.append(
                Rejection(
                    "near_graduation",
                    "bonding curve progress (liquidity freezes at 100%)",
                    prog,
                    c.max_bonding_progress,
                )
            )
        susp, conf = fv.get("creator_suspicion"), fv.get("creator_suspicion_confidence")
        if susp is not None and conf is not None and susp >= 0.6 and conf >= c.creator_suspicion_reject_confidence:
            rej.append(
                Rejection(
                    "suspicious_creator",
                    "creator suspicion (confidence-gated)",
                    f"{susp:.2f}@{conf:.2f}",
                    f">=0.60@{c.creator_suspicion_reject_confidence:.2f}",
                )
            )
        if c.reject_metadata_injection and fv.get("metadata_injection") == 1.0:
            rej.append(
                Rejection("metadata_injection", "token metadata contains instruction-like text", "detected", "none")
            )
        if fv.get("price") is None:
            rej.append(Rejection("no_price", "no executable price", None, "price"))
        return FilterResult(passed=not rej, rejections=rej)
