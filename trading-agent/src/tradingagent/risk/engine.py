"""Portfolio-level risk engine: the final veto before any entry order. Deterministic; nothing here is AI-controlled."""

from __future__ import annotations

from dataclasses import dataclass

from tradingagent.common.config import LiveLimits, RiskConfig
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.portfolio.portfolio import Portfolio
from tradingagent.risk.killswitch import KillSwitch


@dataclass
class RiskCheck:
    approved: bool
    vetoes: list[str]

    def to_dict(self) -> dict:
        return {"approved": self.approved, "vetoes": self.vetoes}


class RiskEngine:
    def __init__(
        self,
        cfg: RiskConfig,
        kill: KillSwitch,
        live_limits: LiveLimits | None = None,
        reentry_cooldown_s: float = 600.0,
    ) -> None:
        self.cfg, self.kill, self.live = cfg, kill, live_limits
        self.reentry_cooldown_s = reentry_cooldown_s
        self.last_exit_at: dict[str, float] = {}

    def note_exit(self, mint: str, t: float) -> None:
        self.last_exit_at[mint] = t

    def check_entry(
        self, pf: Portfolio, mint: str, size_lamports: int, now: float, provider_busy: bool = False
    ) -> RiskCheck:
        c = self.cfg
        v: list[str] = []
        if self.kill.state.killed:
            v.append(f"kill switch active: {self.kill.state.reason}")
        if self.kill.state.paused:
            v.append(f"trading paused: {self.kill.state.pause_reason}")
        if provider_busy:
            v.append("an order for this token is already in flight")
        if pf.open_for(mint) is not None:
            v.append("position already open in this token")
        last = self.last_exit_at.get(mint)
        if last is not None and now - last < self.reentry_cooldown_s:
            v.append(f"re-entry cooldown ({now - last:.0f}s < {self.reentry_cooldown_s:.0f}s)")
        max_open = min(c.max_open_positions, self.live.max_open_positions) if self.live else c.max_open_positions
        if len(pf.positions) >= max_open:
            v.append(f"max open positions reached ({len(pf.positions)}/{max_open})")
        eq = pf.equity
        daily = pf.daily_pnl(now)
        if daily <= -c.max_daily_loss * pf.day_start_equity:
            v.append(f"daily loss limit reached ({daily / LAMPORTS_PER_SOL:.4f} SOL)")
        if self.live and daily <= -self.live.max_daily_loss_sol * LAMPORTS_PER_SOL:
            v.append("live daily loss limit reached")
        if pf.current_drawdown >= c.max_drawdown:
            v.append(f"max drawdown reached ({pf.current_drawdown:.1%})")
        if pf.consecutive_losses >= c.max_consecutive_losses_before_pause:
            v.append(f"{pf.consecutive_losses} consecutive losses: entries paused for review")
        if pf.exposure + size_lamports > c.max_portfolio_exposure * eq:
            v.append("portfolio exposure limit")
        if size_lamports > c.max_position_pct * eq + 1:
            v.append("position size above max_position_pct")
        if pf.cash - size_lamports < (c.min_wallet_balance_sol + c.reserve_sol_for_fees) * LAMPORTS_PER_SOL:
            v.append("would breach minimum wallet balance")
        return RiskCheck(approved=not v, vetoes=v)

    def daily_loss_breached(self, pf: Portfolio, now: float) -> bool:
        return pf.daily_pnl(now) <= -self.cfg.max_daily_loss * pf.day_start_equity
