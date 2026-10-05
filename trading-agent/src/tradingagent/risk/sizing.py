"""Risk-based position sizing with hard ceilings.

    risk_size = equity * max_risk_per_trade / loss_at_stop
    loss_at_stop = hard_stop + entry all-in slippage + exit slippage + network costs

then capped by: max_position_pct, remaining portfolio exposure, cash, pool liquidity share and (in LIVE) the
independent live limits; then scaled DOWN (never up) by volatility, execution confidence, score, daily
drawdown and regime. A higher score can never raise size above the risk-based amount: score 95 does not mean
"invest 95%".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradingagent.common.config import LiveLimits, RiskConfig
from tradingagent.common.units import LAMPORTS_PER_SOL


@dataclass
class SizingInputs:
    equity_sol: float
    cash_sol: float
    open_exposure_sol: float
    daily_pnl_sol: float
    hard_stop_frac: float
    entry_slippage_frac: float
    exit_slippage_frac: float
    network_cost_frac: float
    liquidity_sol: float
    volatility_60s: float | None
    execution_success_rate: float
    score: float
    min_score: float
    regime_multiplier: float


@dataclass
class SizingResult:
    size_sol: float
    binding_constraint: str
    caps: dict[str, float] = field(default_factory=dict)
    multipliers: dict[str, float] = field(default_factory=dict)
    rejected_reason: str | None = None

    @property
    def lamports(self) -> int:
        return int(self.size_sol * LAMPORTS_PER_SOL)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class PositionSizer:
    def __init__(self, cfg: RiskConfig, live_limits: LiveLimits | None = None) -> None:
        self.cfg, self.live = cfg, live_limits

    def size(self, x: SizingInputs) -> SizingResult:
        c = self.cfg
        if x.equity_sol <= 0:
            return SizingResult(0.0, "equity", rejected_reason="no equity")
        loss_at_stop = (
            x.hard_stop_frac + max(x.entry_slippage_frac, 0) + max(x.exit_slippage_frac, 0) + x.network_cost_frac
        )
        loss_at_stop = max(loss_at_stop, 0.01)
        caps = {
            "risk_per_trade": x.equity_sol * c.max_risk_per_trade / loss_at_stop,
            "max_position_pct": x.equity_sol * c.max_position_pct,
            "portfolio_exposure": x.equity_sol * c.max_portfolio_exposure - x.open_exposure_sol,
            "cash": x.cash_sol - c.reserve_sol_for_fees - c.min_wallet_balance_sol,
            "liquidity_share": x.liquidity_sol * c.max_liquidity_share,
        }
        if self.live is not None:
            caps["live_max_position"] = self.live.max_position_sol
            caps["live_max_exposure"] = self.live.max_total_exposure_sol - x.open_exposure_sol
        binding = min(caps, key=lambda k: caps[k])
        base = max(0.0, caps[binding])
        mult: dict[str, float] = {}
        if x.volatility_60s is not None and x.volatility_60s > c.volatility_target_60s:
            mult["volatility"] = c.volatility_target_60s / x.volatility_60s
        mult["execution_confidence"] = max(0.0, min(1.0, x.execution_success_rate))
        span = max(100.0 - x.min_score, 1e-9)
        mult["score"] = max(0.5, min(1.0, 0.5 + 0.5 * (x.score - x.min_score) / span))
        if x.daily_pnl_sol < 0:
            used = -x.daily_pnl_sol / (x.equity_sol * c.max_daily_loss)
            mult["daily_drawdown"] = max(0.0, 1.0 - used)
        mult["regime"] = max(0.0, min(1.0, x.regime_multiplier))
        size = base
        for v in mult.values():
            size *= v
        res = SizingResult(size_sol=size, binding_constraint=binding, caps=caps, multipliers=mult)
        if size < c.min_position_sol:
            res.rejected_reason = f"size {size:.4f} SOL below minimum {c.min_position_sol} (binding: {binding})"
            res.size_sol = 0.0
        return res
