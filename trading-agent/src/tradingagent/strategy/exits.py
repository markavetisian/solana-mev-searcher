"""Exit policy, evaluated on EXECUTABLE value (exact sell quote for the remaining size), never on spot.

Order of precedence: lifecycle (dead/untradeable) > hard stop > thesis invalidation > trailing stop >
take-profit ladder > time stop > max hold.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradingagent.common.config import StrategyConfig
from tradingagent.common.types import ExitReason, LifecycleState
from tradingagent.features.engine import FeatureVector
from tradingagent.market.token import TokenMarket
from tradingagent.portfolio.portfolio import Position
from tradingagent.pump.adapters import QuoteError
from tradingagent.pump.fees import FeeSchedule


@dataclass
class ExitSignal:
    reason: ExitReason
    fraction: float  # of remaining tokens
    detail: str


def mark_position(pos: Position, tm: TokenMarket | None, schedule: FeeSchedule, now: float) -> bool:
    """Update executable value. Returns False if no exit path exists right now."""
    if tm is None:
        return False
    adapter = tm.adapter(schedule)
    if adapter is None or pos.tokens <= 0:
        return False
    try:
        q = adapter.quote_sell(pos.tokens)
    except QuoteError:
        pos.last_value_lamports = 0
        return False
    pos.last_value_lamports = q.amount_out
    pos.last_marked_at = now
    pos.peak_return = max(pos.peak_return, pos.total_return)
    return True


class ExitPolicy:
    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg

    def evaluate(
        self,
        pos: Position,
        tm: TokenMarket | None,
        fv: FeatureVector | None,
        now: float,
        tradeable: bool,
        max_hold_multiplier: float = 1.0,
    ) -> ExitSignal | None:
        c = self.cfg
        if tm is None or tm.record.state in (LifecycleState.UNTRADEABLE, LifecycleState.EXITED):
            return ExitSignal(ExitReason.LIFECYCLE, 1.0, "token no longer tradeable")
        if tm.record.state is LifecycleState.DEAD and tradeable:
            return ExitSignal(ExitReason.DEAD_TOKEN, 1.0, "token marked dead (no activity)")
        if not tradeable:
            return None  # e.g. GRADUATING: no venue until the PumpSwap pool exists; hold and wait
        r = pos.total_return
        held = now - pos.opened_at
        if r <= -c.hard_stop_frac:
            return ExitSignal(ExitReason.HARD_STOP, 1.0, f"executable return {r:.2%} <= -{c.hard_stop_frac:.0%}")
        if fv is not None:
            imb = fv.get("imbalance_60s")
            if imb is not None and imb < c.thesis_imbalance_floor and held > 15:
                return ExitSignal(ExitReason.THESIS_INVALIDATION, 1.0, f"60s buy/sell imbalance {imb:.2f}")
        entry_liq = pos.thesis.get("liquidity_sol")
        if entry_liq:
            cur_liq = tm.liquidity_lamports() / 1e9
            if (
                tm.venue is not None
                and pos.venue.value == tm.venue.value
                and cur_liq < entry_liq * (1 - c.thesis_liquidity_drop_frac)
            ):
                return ExitSignal(
                    ExitReason.THESIS_INVALIDATION, 1.0, f"liquidity fell {1 - cur_liq / entry_liq:.0%} since entry"
                )
        if c.thesis_exit_on_creator_sell and tm.creator_first_sell_at and tm.creator_first_sell_at > pos.opened_at:
            return ExitSignal(ExitReason.THESIS_INVALIDATION, 1.0, "creator started selling after entry")
        if pos.peak_return >= c.trailing_activation_gain and r <= pos.peak_return - c.trailing_stop_frac:
            return ExitSignal(
                ExitReason.TRAILING_STOP,
                1.0,
                f"return {r:.2%} fell {pos.peak_return - r:.2%} from peak {pos.peak_return:.2%}",
            )
        if pos.tp_hits < len(pos.take_profits):
            gain, frac = pos.take_profits[pos.tp_hits]
            if r >= gain:
                return ExitSignal(ExitReason.TAKE_PROFIT, min(1.0, frac), f"TP{pos.tp_hits + 1} at +{gain:.0%}")
        if held > c.time_stop_s and r < c.time_stop_min_gain:
            return ExitSignal(ExitReason.TIME_STOP, 1.0, f"thesis did not develop in {c.time_stop_s:.0f}s ({r:.2%})")
        if held > c.max_hold_s * max_hold_multiplier:
            return ExitSignal(ExitReason.TIME_STOP, 1.0, f"max hold {c.max_hold_s * max_hold_multiplier:.0f}s")
        return None
