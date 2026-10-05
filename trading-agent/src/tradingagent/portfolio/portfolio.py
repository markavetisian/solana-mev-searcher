"""Portfolio accounting in lamports. Positions are marked at EXECUTABLE value (exact sell quote for the full
remaining size, including our own price impact and fees) — never at displayed spot price."""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from tradingagent.common.types import ExitReason, Venue
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.execution.orders import ExecutionReport


@dataclass
class Fill:
    t: float
    execution_id: str
    tokens: int
    lamports: int  # paid (entry) or received (exit), net of platform fees
    network_fees: int
    spot: float | None
    reason: str


@dataclass
class Position:
    position_id: str
    mint: str
    symbol: str | None
    venue: Venue
    opened_at: float
    decision_id: str | None
    tokens: int
    initial_tokens: int
    cost_lamports: int  # remaining cost basis (incl. entry fees)
    initial_cost_lamports: int
    entry_spot_price: float
    entry_effective_price: float
    expected_entry_slippage: float
    actual_entry_slippage: float
    hard_stop_frac: float
    take_profits: list[tuple[float, float]]
    trailing_stop_frac: float
    trailing_activation_gain: float
    max_hold_s: float
    score: float
    regime: str
    thesis: dict[str, Any]
    versions: dict[str, str]
    realized_lamports: int = 0
    network_fees: int = 0
    platform_fees: int = 0
    peak_return: float = 0.0
    last_value_lamports: int = 0
    last_marked_at: float = 0.0
    current_thesis: dict[str, Any] = field(default_factory=dict)
    exits: list[Fill] = field(default_factory=list)
    entry: Fill | None = None
    status: str = "OPEN"  # OPEN | CLOSING | CLOSED | STUCK
    exit_reason: str | None = None
    closed_at: float | None = None
    tp_hits: int = 0
    stuck_reason: str | None = None
    pending_exit_id: str | None = None
    entry_spot_for_outcome: float = 0.0
    exit_spot_for_outcome: float | None = None
    gross_tokens_sold: int = 0
    gross_spot_sum: float = 0.0  # sum(tokens_sold * spot_at_exit) — token-weighted exit spot

    def record_exit_spot(self, tokens: int, spot: float | None) -> None:
        self.gross_tokens_sold += tokens
        self.gross_spot_sum += tokens * (spot or 0.0)

    @property
    def gross_return(self) -> float:
        """Spot-to-spot return, token-weighted over all exits (excludes fees and our own impact)."""
        if not self.entry_spot_for_outcome or not self.gross_tokens_sold:
            return 0.0
        return (self.gross_spot_sum / self.gross_tokens_sold) / self.entry_spot_for_outcome - 1

    @property
    def total_return(self) -> float:
        """(realized + executable value of remainder - network fees) / initial cost - 1."""
        if self.initial_cost_lamports <= 0:
            return 0.0
        return (self.realized_lamports + self.last_value_lamports - self.network_fees) / self.initial_cost_lamports - 1

    @property
    def unrealized_lamports(self) -> int:
        return self.last_value_lamports - self.cost_lamports

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "mint": self.mint,
            "symbol": self.symbol,
            "venue": self.venue.value,
            "opened_at": self.opened_at,
            "status": self.status,
            "tokens": self.tokens,
            "initial_tokens": self.initial_tokens,
            "cost_sol": self.cost_lamports / LAMPORTS_PER_SOL,
            "initial_cost_sol": self.initial_cost_lamports / LAMPORTS_PER_SOL,
            "entry_spot_price": self.entry_spot_price,
            "entry_effective_price": self.entry_effective_price,
            "expected_entry_slippage": self.expected_entry_slippage,
            "actual_entry_slippage": self.actual_entry_slippage,
            "value_sol": self.last_value_lamports / LAMPORTS_PER_SOL,
            "realized_sol": self.realized_lamports / LAMPORTS_PER_SOL,
            "unrealized_sol": self.unrealized_lamports / LAMPORTS_PER_SOL,
            "total_return": self.total_return,
            "peak_return": self.peak_return,
            "hard_stop_frac": self.hard_stop_frac,
            "take_profits": self.take_profits,
            "tp_hits": self.tp_hits,
            "trailing_stop_frac": self.trailing_stop_frac,
            "trailing_stop_level": (self.peak_return - self.trailing_stop_frac)
            if self.peak_return >= self.trailing_activation_gain
            else None,
            "network_fees_sol": self.network_fees / LAMPORTS_PER_SOL,
            "platform_fees_sol": self.platform_fees / LAMPORTS_PER_SOL,
            "score": self.score,
            "regime": self.regime,
            "thesis": self.thesis,
            "current_thesis": self.current_thesis,
            "versions": self.versions,
            "exit_reason": self.exit_reason,
            "closed_at": self.closed_at,
            "decision_id": self.decision_id,
            "stuck_reason": self.stuck_reason,
            "entry_execution_id": self.entry.execution_id if self.entry else None,
            "exits": [f.__dict__ for f in self.exits],
        }


@dataclass
class ClosedTrade:
    position_id: str
    mint: str
    symbol: str | None
    venue: str
    opened_at: float
    closed_at: float
    cost_lamports: int
    proceeds_lamports: int
    network_fees: int
    platform_fees: int
    pnl_lamports: int
    net_return: float
    gross_return: float  # spot-to-spot (for the EV model)
    hold_s: float
    exit_reason: str
    score: float
    regime: str
    expected_entry_slippage: float
    actual_entry_slippage: float
    versions: dict[str, str]
    decision_id: str | None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Portfolio:
    def __init__(self, starting_lamports: int, clock_now: float | None = None) -> None:
        self.starting_lamports = starting_lamports
        self.cash = starting_lamports
        self.positions: dict[str, Position] = {}
        self.closed: deque[ClosedTrade] = deque(maxlen=20_000)
        self.realized_pnl = 0
        self.fees_paid = 0
        self.high_water = starting_lamports
        self.max_drawdown = 0.0
        self.day_key: str | None = None
        self.day_start_equity = starting_lamports
        self.consecutive_losses = 0
        self.failed_executions = 0
        self.total_executions = 0
        self._roll_day(clock_now or time.time())

    # ---- valuation ----------------------------------------------------------------------------------------
    @property
    def positions_value(self) -> int:
        return sum(p.last_value_lamports for p in self.positions.values())

    @property
    def equity(self) -> int:
        return self.cash + self.positions_value

    @property
    def exposure(self) -> int:
        return sum(p.cost_lamports for p in self.positions.values())

    def open_for(self, mint: str) -> Position | None:
        for p in self.positions.values():
            if p.mint == mint:
                return p
        return None

    def _roll_day(self, now: float) -> None:
        key = time.strftime("%Y-%m-%d", time.gmtime(now))
        if key != self.day_key:
            self.day_key = key
            self.day_start_equity = self.equity if self.day_key is not None else self.starting_lamports

    def daily_pnl(self, now: float) -> int:
        self._roll_day(now)
        return self.equity - self.day_start_equity

    def update_drawdown(self) -> None:
        eq = self.equity
        self.high_water = max(self.high_water, eq)
        if self.high_water > 0:
            self.max_drawdown = max(self.max_drawdown, 1 - eq / self.high_water)

    @property
    def current_drawdown(self) -> float:
        return 1 - self.equity / self.high_water if self.high_water > 0 else 0.0

    @property
    def execution_success_rate(self) -> float:
        if self.total_executions < 5:
            return 1.0
        return 1 - self.failed_executions / self.total_executions

    # ---- mutations ----------------------------------------------------------------------------------------
    def record_execution(self, ok: bool) -> None:
        self.total_executions += 1
        if not ok:
            self.failed_executions += 1

    def open_position(self, rep: ExecutionReport, now: float, **kw: Any) -> Position:
        cost = rep.actual_in  # lamports paid incl. platform fees
        self.cash -= cost + rep.network_fees
        self.fees_paid += rep.network_fees + rep.platform_fees
        pid = "pos_" + uuid.uuid4().hex[:16]
        spot = rep.spot_at_fill or rep.expected_price
        pos = Position(
            position_id=pid,
            mint=rep.mint,
            venue=rep.venue,
            opened_at=now,
            tokens=rep.actual_out,
            initial_tokens=rep.actual_out,
            cost_lamports=cost,
            initial_cost_lamports=cost,
            entry_spot_price=spot,
            entry_effective_price=rep.actual_price,
            actual_entry_slippage=(rep.actual_price / spot - 1) if spot else 0.0,
            network_fees=rep.network_fees,
            platform_fees=rep.platform_fees,
            entry_spot_for_outcome=spot,
            **kw,
        )
        pos.entry = Fill(now, rep.execution_id, rep.actual_out, cost, rep.network_fees, spot, "entry")
        pos.last_value_lamports = cost
        self.positions[pid] = pos
        self.update_drawdown()
        return pos

    def apply_exit(self, pos: Position, rep: ExecutionReport, now: float, reason: str) -> ClosedTrade | None:
        tokens = min(rep.actual_in, pos.tokens)
        if tokens <= 0:
            return None
        cost_part = pos.cost_lamports * tokens // pos.tokens
        pos.tokens -= tokens
        pos.cost_lamports -= cost_part
        pos.realized_lamports += rep.actual_out
        pos.network_fees += rep.network_fees
        pos.platform_fees += rep.platform_fees
        self.cash += rep.actual_out - rep.network_fees
        self.fees_paid += rep.network_fees + rep.platform_fees
        pos.exits.append(
            Fill(now, rep.execution_id, tokens, rep.actual_out, rep.network_fees, rep.spot_at_fill, reason)
        )
        pos.record_exit_spot(tokens, rep.spot_at_fill)
        pos.exit_spot_for_outcome = rep.spot_at_fill
        pos.pending_exit_id = None
        if pos.tokens <= 0 or pos.tokens < pos.initial_tokens * 1e-6:
            return self._close(pos, now, reason)
        pos.status = "OPEN"
        self.update_drawdown()
        return None

    def _close(self, pos: Position, now: float, reason: str) -> ClosedTrade:
        pos.status, pos.exit_reason, pos.closed_at = "CLOSED", reason, now
        pos.last_value_lamports = 0
        pnl = pos.realized_lamports - pos.initial_cost_lamports - pos.network_fees
        self.realized_pnl += pnl
        self.positions.pop(pos.position_id, None)
        net = pnl / pos.initial_cost_lamports if pos.initial_cost_lamports else 0.0
        gross = pos.gross_return
        self.consecutive_losses = self.consecutive_losses + 1 if pnl <= 0 else 0
        ct = ClosedTrade(
            position_id=pos.position_id,
            mint=pos.mint,
            symbol=pos.symbol,
            venue=pos.venue.value,
            opened_at=pos.opened_at,
            closed_at=now,
            cost_lamports=pos.initial_cost_lamports,
            proceeds_lamports=pos.realized_lamports,
            network_fees=pos.network_fees,
            platform_fees=pos.platform_fees,
            pnl_lamports=pnl,
            net_return=net,
            gross_return=gross,
            hold_s=now - pos.opened_at,
            exit_reason=reason,
            score=pos.score,
            regime=pos.regime,
            expected_entry_slippage=pos.expected_entry_slippage,
            actual_entry_slippage=pos.actual_entry_slippage,
            versions=pos.versions,
            decision_id=pos.decision_id,
        )
        self.closed.append(ct)
        self.update_drawdown()
        return ct

    def write_off(self, pos: Position, now: float, reason: str = ExitReason.DEAD_TOKEN.value) -> ClosedTrade:
        """Token became untradeable with no exit path: book the remainder at zero."""
        pos.record_exit_spot(pos.tokens, 0.0)
        return self._close(pos, now, reason)

    def snapshot(self, now: float) -> dict:
        self.update_drawdown()
        return {
            "t": now,
            "equity_sol": self.equity / LAMPORTS_PER_SOL,
            "cash_sol": self.cash / LAMPORTS_PER_SOL,
            "positions_value_sol": self.positions_value / LAMPORTS_PER_SOL,
            "exposure_sol": self.exposure / LAMPORTS_PER_SOL,
            "realized_pnl_sol": self.realized_pnl / LAMPORTS_PER_SOL,
            "unrealized_pnl_sol": sum(p.unrealized_lamports for p in self.positions.values()) / LAMPORTS_PER_SOL,
            "daily_pnl_sol": self.daily_pnl(now) / LAMPORTS_PER_SOL,
            "drawdown": self.current_drawdown,
            "max_drawdown": self.max_drawdown,
            "open_positions": len(self.positions),
            "fees_paid_sol": self.fees_paid / LAMPORTS_PER_SOL,
            "closed_trades": len(self.closed),
            "consecutive_losses": self.consecutive_losses,
            "execution_success_rate": self.execution_success_rate,
            "starting_equity_sol": self.starting_lamports / LAMPORTS_PER_SOL,
        }
