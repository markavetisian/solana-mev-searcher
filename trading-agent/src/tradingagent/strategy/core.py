"""TradingCore: one object that owns the whole decision path and is shared verbatim by the live runtime
(PAPER / SHADOW / LIVE) and the backtester. Only the clock, the event source and the execution provider differ.

It is synchronous; async I/O (AI calls, order execution, DB writes) is done by the caller.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from tradingagent.ai.analyst import AIResult
from tradingagent.common.clock import Clock
from tradingagent.common.config import AppConfig, LiveLimits
from tradingagent.common.events import MarketEvent
from tradingagent.common.logging import get_logger
from tradingagent.common.metrics import METRICS
from tradingagent.common.types import (
    TRADEABLE_STATES,
    DecisionOutcome,
    EventKind,
    ExecStatus,
    ExitReason,
    LifecycleState,
    Mode,
    Side,
)
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.execution.orders import ExecutionReport, OrderIntent, new_execution_id
from tradingagent.execution.simulator import ExecutionSimulator
from tradingagent.features.engine import FeatureEngine
from tradingagent.market.engine import MarketDataEngine
from tradingagent.portfolio.portfolio import ClosedTrade, Portfolio, Position
from tradingagent.pump.adapters import MarketAdapter, QuoteError
from tradingagent.pump.fees import FeeSchedule
from tradingagent.risk.engine import RiskEngine
from tradingagent.risk.filters import TokenRiskFilter
from tradingagent.risk.killswitch import KillSwitch
from tradingagent.risk.sizing import PositionSizer
from tradingagent.scoring.model import Scorer
from tradingagent.strategy.decision import Decision, DecisionEngine
from tradingagent.strategy.ev import EVModel, OutcomeSample, OutcomeStore
from tradingagent.strategy.exits import ExitPolicy, ExitSignal, mark_position
from tradingagent.strategy.regime import RegimeDetector
from tradingagent.wallets.entities import CreatorBook, EntityGraph, WalletBook

log = get_logger("strategy.core")


class Sink(Protocol):
    def decision(self, d: Decision) -> None: ...
    def execution(self, intent: OrderIntent, rep: ExecutionReport) -> None: ...
    def position(self, pos: Position, event: str, detail: dict | None = None) -> None: ...
    def trade(self, ct: ClosedTrade) -> None: ...
    def risk_event(self, kind: str, detail: dict) -> None: ...
    def outcome(self, s: OutcomeSample) -> None: ...


class NullSink:
    def decision(self, d: Decision) -> None: ...
    def execution(self, intent: OrderIntent, rep: ExecutionReport) -> None: ...
    def position(self, pos: Position, event: str, detail: dict | None = None) -> None: ...
    def trade(self, ct: ClosedTrade) -> None: ...
    def risk_event(self, kind: str, detail: dict) -> None: ...
    def outcome(self, s: OutcomeSample) -> None: ...


class MultiSink:
    def __init__(self, *sinks: Any) -> None:
        self.sinks = [s for s in sinks if s is not None]

    def __getattr__(self, name: str) -> Callable[..., None]:
        def fan(*a: Any, **k: Any) -> None:
            for s in self.sinks:
                fn = getattr(s, name, None)
                if fn:
                    fn(*a, **k)

        return fan


@dataclass
class VirtualPosition:
    """Calibration position: follows the exit policy on live data with no capital, to measure the signal."""

    pos: Position
    score: float
    regime: str


@dataclass
class TradingCore:
    cfg: AppConfig
    clock: Clock
    mode: Mode
    schedule: FeeSchedule
    market: MarketDataEngine
    wallets: WalletBook
    creators: CreatorBook
    entities: EntityGraph
    features: FeatureEngine
    decisions: DecisionEngine
    exits: ExitPolicy
    portfolio: Portfolio
    risk: RiskEngine
    kill: KillSwitch
    outcomes: OutcomeStore
    regime: RegimeDetector
    sink: Any = field(default_factory=NullSink)
    calibration_enabled: bool = True
    calibration_size_sol: float = 0.25
    outcome_source: str = "paper"
    _due: dict[str, float] = field(default_factory=dict)
    _last_eval: dict[str, float] = field(default_factory=dict)
    _launch_linked: set[str] = field(default_factory=set)
    virtual: dict[str, VirtualPosition] = field(default_factory=dict)
    _last_regime_eval: float = 0.0
    last_decisions: dict[str, Decision] = field(default_factory=dict)

    # ---- ingestion ---------------------------------------------------------------------------------------
    def ingest(self, ev: MarketEvent) -> None:
        tm = self.market.on_event(ev)
        if tm is None:
            return
        rec = tm.record
        if ev.kind is EventKind.CREATE and rec.creator:
            self.creators.on_create(rec.creator, rec.mint, ev.t)
        elif ev.kind in (EventKind.TRADE, EventKind.AMM_TRADE):
            self.wallets.on_trade(ev, rec.birth_time if not rec.partial_history else None)
            self.creators.on_trade(ev, rec.creator, tm.market_cap_lamports())
            if rec.created_slot is not None and rec.mint not in self._launch_linked and ev.slot > rec.created_slot + 2:
                self._launch_linked.add(rec.mint)
                if tm.launch_slot_buyers:
                    self.entities.on_launch_slot_buyers(rec.creator, tm.launch_slot_buyers)
            self._due[rec.mint] = ev.t
        elif ev.kind in (EventKind.COMPLETE, EventKind.MIGRATE):
            self.creators.on_graduate(rec.mint)

    # ---- evaluation scheduling ---------------------------------------------------------------------------
    def due_candidates(self, now: float) -> list[str]:
        out: list[str] = []
        cool = self.cfg.strategy.evaluation_cooldown_s
        min_tr = min(self.cfg.research.candidate_min_trades_60s, self.cfg.filters.min_trades_60s)
        for mint in list(self._due):
            tm = self.market.market(mint)
            if tm is None or tm.record.state not in TRADEABLE_STATES:
                self._due.pop(mint, None)
                continue
            if now - self._last_eval.get(mint, -1e18) < cool:
                continue
            self._due.pop(mint, None)
            if len(tm.series.window(now, 60)) < min_tr:
                continue
            self._last_eval[mint] = now
            out.append(mint)
        return out

    def refresh_regime(self, now: float, rpc_healthy: bool = True, feed_connected: bool = True) -> None:
        if now - self._last_regime_eval >= 5.0:
            self.regime.evaluate(self.market, now, rpc_healthy, feed_connected)
            self._last_regime_eval = now

    def stage1(self, mint: str, now: float, prio: int) -> Decision:
        return self.decisions.stage1(mint, now, prio)

    def stage2(self, d: Decision, ai: AIResult, now: float, prio: int, provider_busy: bool = False) -> Decision:
        d = self.decisions.stage2(d, ai, now, prio, provider_busy)
        if not self.kill.entries_allowed and d.intent is not None:
            d.intent = None
            d.outcome = DecisionOutcome.REJECTED
        return d

    def finish_decision(self, d: Decision, now: float) -> None:
        self.last_decisions[d.mint] = d
        if len(self.last_decisions) > 2000:
            for k in list(self.last_decisions)[:500]:
                del self.last_decisions[k]
        self.sink.decision(d)
        if d.calibration_candidate and self.calibration_enabled:
            self._open_virtual(d, now)

    # ---- calibration (virtual) positions -------------------------------------------------------------------
    def _open_virtual(self, d: Decision, now: float) -> None:
        if d.mint in self.virtual or d.score is None:
            return
        tm = self.market.market(d.mint)
        adapter = tm.adapter(self.schedule) if tm else None
        if adapter is None:
            return
        try:
            q = adapter.quote_buy(int(self.calibration_size_sol * LAMPORTS_PER_SOL))
        except QuoteError:
            return
        pos = self._new_position(
            d, q.amount_in, q.amount_out, q.spot_price_before, q.effective_price, now, venue=adapter.venue, virtual=True
        )
        self.virtual[d.mint] = VirtualPosition(
            pos=pos, score=d.score.total, regime=d.regime.regime.value if d.regime else "UNKNOWN"
        )

    def calibration_step(self, now: float) -> None:
        staleness = self.market.activity.staleness(now)
        for mint, vp in list(self.virtual.items()):
            pos = vp.pos
            tm = self.market.market(mint)
            tradeable = mark_position(pos, tm, self.schedule, now)
            fv = self.features.compute(tm, now, staleness) if (tm is not None and tradeable) else None
            sig = self.exits.evaluate(pos, tm, fv, now, tradeable)
            if sig is None:
                continue
            spot = (tm.spot_price() or 0.0) if (tm is not None and tradeable) else 0.0
            if sig.fraction < 0.999 and tradeable:
                sold = max(1, int(pos.tokens * sig.fraction))
                pos.record_exit_spot(sold, spot)
                adapter = tm.adapter(self.schedule) if tm else None
                try:
                    pos.realized_lamports += adapter.quote_sell(sold).amount_out if adapter else 0
                except QuoteError:
                    pass
                pos.cost_lamports -= pos.cost_lamports * sold // max(pos.tokens, 1)
                pos.tokens -= sold
                pos.tp_hits += 1
                continue
            mark_position(pos, tm, self.schedule, now)
            pos.record_exit_spot(pos.tokens, spot)
            s = OutcomeSample(
                t=pos.opened_at,
                score=vp.score,
                regime=vp.regime,
                gross_return=pos.gross_return,
                net_return=pos.total_return,
                hold_s=now - pos.opened_at,
                source="calibration",
                strategy_version=self.decisions.versions["strategy_version"],
            )
            self.outcomes.add(s)
            self.sink.outcome(s)
            del self.virtual[mint]

    # ---- positions -----------------------------------------------------------------------------------------
    def _new_position(
        self,
        d: Decision,
        cost: int,
        tokens: int,
        spot: float,
        eff: float,
        now: float,
        venue: Any,
        virtual: bool = False,
    ) -> Position:
        c = self.cfg.strategy
        ov = self.regime.override(d.regime.regime) if d.regime else None
        fv = d.features
        thesis = {
            "score": d.score.total if d.score else None,
            "liquidity_sol": fv.get("liquidity_sol") if fv else None,
            "imbalance_60s": fv.get("imbalance_60s") if fv else None,
            "ret_60s": fv.get("ret_60s") if fv else None,
            "holders": fv.get("holders") if fv else None,
            "regime": d.regime.regime.value if d.regime else None,
            "ai_thesis": d.ai.assessment.thesis if (d.ai and d.ai.assessment) else None,
        }
        return Position(
            position_id=("vpos_" if virtual else "pos_") + d.decision_id[-12:],
            mint=d.mint,
            symbol=d.symbol,
            venue=venue,
            opened_at=now,
            decision_id=d.decision_id,
            tokens=tokens,
            initial_tokens=tokens,
            cost_lamports=cost,
            initial_cost_lamports=cost,
            entry_spot_price=spot,
            entry_effective_price=eff,
            expected_entry_slippage=d.execution.expected_slippage if d.execution else 0.0,
            actual_entry_slippage=(eff / spot - 1) if spot else 0.0,
            hard_stop_frac=c.hard_stop_frac,
            take_profits=list(c.take_profit_levels),
            trailing_stop_frac=c.trailing_stop_frac,
            trailing_activation_gain=c.trailing_activation_gain,
            max_hold_s=c.max_hold_s * (ov.max_hold_multiplier if ov else 1.0),
            score=d.score.total if d.score else 0.0,
            regime=d.regime.regime.value if d.regime else "UNKNOWN",
            thesis=thesis,
            versions=dict(d.versions),
            last_value_lamports=cost,
            entry_spot_for_outcome=spot,
        )

    def on_entry_report(self, d: Decision, intent: OrderIntent, rep: ExecutionReport, now: float) -> Position | None:
        self.sink.execution(intent, rep)
        self.portfolio.record_execution(rep.filled)
        self._execution_safety(intent, rep)
        if not rep.filled:
            if rep.network_fees:
                self.portfolio.cash -= rep.network_fees
                self.portfolio.fees_paid += rep.network_fees
            return None
        pos = self.portfolio.open_position(
            rep,
            now,
            symbol=d.symbol,
            decision_id=d.decision_id,
            expected_entry_slippage=intent.expected_slippage,
            hard_stop_frac=self.cfg.strategy.hard_stop_frac,
            take_profits=list(self.cfg.strategy.take_profit_levels),
            trailing_stop_frac=self.cfg.strategy.trailing_stop_frac,
            trailing_activation_gain=self.cfg.strategy.trailing_activation_gain,
            max_hold_s=self.cfg.strategy.max_hold_s * self.regime.override(d.regime.regime).max_hold_multiplier
            if d.regime
            else self.cfg.strategy.max_hold_s,
            score=d.score.total if d.score else 0.0,
            regime=d.regime.regime.value if d.regime else "UNKNOWN",
            thesis=self._new_position(
                d, rep.actual_in, rep.actual_out, rep.spot_at_fill or 0, rep.actual_price, now, rep.venue
            ).thesis,
            versions=dict(d.versions),
        )
        self.sink.position(pos, "OPENED", {"execution": rep.to_dict()})
        return pos

    def _execution_safety(self, intent: OrderIntent, rep: ExecutionReport) -> None:
        if rep.status is ExecStatus.UNCERTAIN:
            self.kill.uncertain_transaction(intent.execution_id)
            self.sink.risk_event("UNCERTAIN_TX", {"execution_id": intent.execution_id})
        elif rep.status is ExecStatus.FAILED:
            self.kill.record_execution_failure(rep.error or "failed")
            self.sink.risk_event("EXECUTION_FAILED", {"execution_id": intent.execution_id, "error": rep.error})
        elif rep.filled:
            self.kill.record_slippage(
                max(rep.slippage_vs_expected, 0.0) + max(intent.expected_slippage, 0.0),
                max(intent.expected_slippage, 0.0),
            )

    def exit_intents(
        self, now: float, prio: int, busy: Callable[[str], bool]
    ) -> list[tuple[Position, ExitSignal, OrderIntent]]:
        out = []
        if not self.kill.orders_allowed:
            return out
        for pos in list(self.portfolio.positions.values()):
            if pos.pending_exit_id or busy(pos.mint):
                continue
            tm = self.market.market(pos.mint)
            tradeable = mark_position(pos, tm, self.schedule, now)
            if not tradeable and tm is not None and tm.record.state is LifecycleState.GRADUATING:
                pos.status, pos.stuck_reason = "STUCK", "graduating: no venue until PumpSwap pool exists"
                continue
            if not tradeable and (tm is None or tm.record.state in (LifecycleState.UNTRADEABLE, LifecycleState.EXITED)):
                ct = self.portfolio.write_off(pos, now)
                self.sink.trade(ct)
                self._record_outcome(ct)
                continue
            fv = None
            if tm is not None and tradeable:
                fv = self.features.compute(tm, now, self.market.activity.staleness(now))
                pos.current_thesis = {
                    "imbalance_60s": fv.get("imbalance_60s"),
                    "ret_60s": fv.get("ret_60s"),
                    "liquidity_sol": fv.get("liquidity_sol"),
                    "holders": fv.get("holders"),
                }
            ov = self.regime.override(self.regime.current.regime)
            sig = self.exits.evaluate(pos, tm, fv, now, tradeable, ov.max_hold_multiplier)
            if sig is None:
                continue
            intent = self.build_exit_intent(pos, sig, now, prio)
            if intent is not None:
                pos.pending_exit_id = intent.execution_id
                pos.status = "CLOSING"
                out.append((pos, sig, intent))
        return out

    def build_exit_intent(self, pos: Position, sig: ExitSignal, now: float, prio: int) -> OrderIntent | None:
        tm = self.market.market(pos.mint)
        adapter: MarketAdapter | None = tm.adapter(self.schedule) if tm else None
        if adapter is None or tm is None:
            return None
        tokens = pos.tokens if sig.fraction >= 0.999 else max(1, int(pos.tokens * sig.fraction))
        est = self.decisions.sim.simulate_exit(adapter, tokens, prio)
        if not est.ok:
            return None
        # Exits must not be blocked by a tight slippage limit when the stop is hit; widen for protective exits.
        protective = sig.reason in (
            ExitReason.HARD_STOP,
            ExitReason.THESIS_INVALIDATION,
            ExitReason.LIFECYCLE,
            ExitReason.DEAD_TOKEN,
            ExitReason.KILL_SWITCH,
        )
        slip = self.cfg.execution.max_slippage_bps / 10_000 * (3 if protective else 1)
        return OrderIntent(
            execution_id=new_execution_id(),
            mint=pos.mint,
            side=Side.SELL,
            venue=adapter.venue,
            amount_in=tokens,
            min_out=int(est.expected_output * (1 - min(slip, 0.5))),
            expected_out=est.expected_output,
            expected_price=est.expected_price,
            spot_price=est.quoted_price,
            expected_slippage=est.expected_slippage,
            priority_micro_lamports=prio,
            reason=f"{sig.reason.value}: {sig.detail}",
            created_at=now,
            position_id=pos.position_id,
            token_program=tm.record.token_program,
            creator=(tm.curve.creator if tm.curve else tm.record.creator),
            pool=tm.record.pool_address,
            is_exit=True,
            meta={"full_exit": tokens >= pos.tokens, "mayhem": tm.record.is_mayhem},
        )

    def on_exit_report(
        self, pos: Position, sig: ExitSignal, intent: OrderIntent, rep: ExecutionReport, now: float
    ) -> ClosedTrade | None:
        self.sink.execution(intent, rep)
        self.portfolio.record_execution(rep.filled)
        self._execution_safety(intent, rep)
        if not rep.filled:
            pos.pending_exit_id = None
            pos.status = "OPEN"
            if rep.network_fees:
                self.portfolio.cash -= rep.network_fees
                pos.network_fees += rep.network_fees
            return None
        if sig.reason is ExitReason.TAKE_PROFIT:
            pos.tp_hits += 1
        ct = self.portfolio.apply_exit(pos, rep, now, sig.reason.value)
        self.sink.position(pos, "CLOSED" if ct else "PARTIAL_EXIT", {"reason": sig.detail, "execution": rep.to_dict()})
        if ct is not None:
            self.risk.note_exit(pos.mint, now)
            self.sink.trade(ct)
            self._record_outcome(ct)
        return ct

    def _record_outcome(self, ct: ClosedTrade) -> None:
        s = OutcomeSample(
            t=ct.opened_at,
            score=ct.score,
            regime=ct.regime,
            gross_return=ct.gross_return,
            net_return=ct.net_return,
            hold_s=ct.hold_s,
            source=self.outcome_source,
            strategy_version=ct.versions.get("strategy_version", ""),
        )
        self.outcomes.add(s)
        self.sink.outcome(s)

    def periodic_risk(self, now: float) -> None:
        pf = self.portfolio
        if self.risk.daily_loss_breached(pf, now) and not self.kill.state.killed:
            self.kill.activate(f"daily loss limit reached ({pf.daily_pnl(now) / LAMPORTS_PER_SOL:.4f} SOL)")
            self.sink.risk_event("DAILY_LOSS_LIMIT", {"daily_pnl_sol": pf.daily_pnl(now) / LAMPORTS_PER_SOL})
        if pf.current_drawdown >= self.cfg.risk.max_drawdown and not self.kill.state.killed:
            self.kill.activate(f"max drawdown {pf.current_drawdown:.1%} reached")
        if self.mode is not Mode.RESEARCH:
            self.kill.observe_staleness(self.market.activity.staleness(now), bool(pf.positions))

    def emergency_exit_intents(self, now: float, prio: int) -> list[tuple[Position, ExitSignal, OrderIntent]]:
        out = []
        for pos in list(self.portfolio.positions.values()):
            if pos.pending_exit_id:
                continue
            sig = ExitSignal(ExitReason.KILL_SWITCH, 1.0, "emergency exit on kill switch")
            intent = self.build_exit_intent(pos, sig, now, prio)
            if intent:
                pos.pending_exit_id = intent.execution_id
                out.append((pos, sig, intent))
        return out


def build_core(
    cfg: AppConfig,
    clock: Clock,
    mode: Mode,
    sink: Any = None,
    live_limits: LiveLimits | None = None,
    outcomes: OutcomeStore | None = None,
    starting_equity_sol: float | None = None,
    schedule: FeeSchedule | None = None,
    ai_model_name: str | None = None,
) -> TradingCore:
    schedule = schedule or FeeSchedule.from_rows(cfg.pump.fee_tiers, cfg.pump.flat_fees)
    market = MarketDataEngine(cfg, clock, schedule)
    wallets, creators, entities = WalletBook(), CreatorBook(), EntityGraph()
    features = FeatureEngine(
        schedule,
        wallets,
        creators,
        entities,
        ref_size_sol=max(cfg.risk.min_position_sol, 0.25),
        bundle_threshold=cfg.filters.max_same_slot_launch_buyers,
    )
    now = clock.now() if clock.now() > 0 else time.time()
    pf = Portfolio(int((starting_equity_sol or cfg.paper.starting_equity_sol) * LAMPORTS_PER_SOL), now)
    kill = KillSwitch(cfg.killswitch, clock.now)
    risk = RiskEngine(cfg.risk, kill, live_limits if mode is Mode.LIVE else None, cfg.strategy.reentry_cooldown_s)
    outcomes = outcomes or OutcomeStore()
    regime = RegimeDetector(cfg.regime)
    dec = DecisionEngine(
        cfg,
        market,
        features,
        Scorer(cfg.scoring),
        TokenRiskFilter(cfg.filters),
        regime,
        EVModel(cfg.ev, outcomes),
        PositionSizer(cfg.risk, live_limits if mode is Mode.LIVE else None),
        ExecutionSimulator(cfg.execution),
        risk,
        pf,
        live_limits,
        ai_model_name,
    )
    src = {Mode.PAPER: "paper", Mode.SHADOW: "shadow", Mode.LIVE: "live", Mode.RESEARCH: "calibration"}[mode]
    core = TradingCore(
        cfg=cfg,
        clock=clock,
        mode=mode,
        schedule=schedule,
        market=market,
        wallets=wallets,
        creators=creators,
        entities=entities,
        features=features,
        decisions=dec,
        exits=ExitPolicy(cfg.strategy),
        portfolio=pf,
        risk=risk,
        kill=kill,
        outcomes=outcomes,
        regime=regime,
        sink=sink or NullSink(),
        outcome_source=src,
        calibration_size_sol=max(cfg.risk.min_position_sol, 0.25),
    )
    METRICS.set("core.built", 1)
    return core
