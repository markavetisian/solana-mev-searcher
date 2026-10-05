"""Event-driven backtester.

Look-ahead prevention is structural:
  * Events are replayed strictly in (observed_at, slot, signature, seq) order; the ManualClock only moves forward.
  * The TradingCore only ever holds state built from events already replayed — there is no API to read ahead.
  * Decisions happen on a fixed evaluation tick; orders fill `assumed_latency_s` LATER against the venue state
    at that later time (other traders' flow in between included), with fees, exact impact, extra adverse drift,
    random failures and min_out reverts.
  * Outcomes feed the EV model only after the trade has closed (in event time).
  * Survivorship: the raw event stream contains every token that ever traded, dead or alive; nothing is filtered
    by later fate. Tokens first seen mid-life are flagged partial_history.
AI is disabled in backtests: an LLM cannot be replayed honestly (knowledge leakage, non-determinism, cost). It is
evaluated in paper/shadow forward tests instead.
"""

from __future__ import annotations

import heapq
import itertools
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from tradingagent.ai.analyst import AIResult
from tradingagent.common.clock import ManualClock
from tradingagent.common.config import AppConfig
from tradingagent.common.events import MarketEvent
from tradingagent.common.logging import get_logger
from tradingagent.common.types import DecisionOutcome, ExitReason, Mode
from tradingagent.execution.providers import PaperFillModel
from tradingagent.portfolio.portfolio import ClosedTrade
from tradingagent.strategy.core import TradingCore, build_core
from tradingagent.strategy.decision import Decision
from tradingagent.strategy.ev import OutcomeSample, OutcomeStore
from tradingagent.strategy.exits import ExitSignal

log = get_logger("backtest")


class SyntheticDataRefused(ValueError):
    pass


@dataclass
class CollectingSink:
    decisions: Counter = field(default_factory=Counter)
    rejections: Counter = field(default_factory=Counter)
    trades: list[ClosedTrade] = field(default_factory=list)
    outcomes: list[OutcomeSample] = field(default_factory=list)
    executions: Counter = field(default_factory=Counter)
    keep_decisions: int = 0
    sample_decisions: list[dict] = field(default_factory=list)

    def decision(self, d: Decision) -> None:
        self.decisions[d.outcome.value] += 1
        for g in d.gates:
            if g.status == "FAIL":
                self.rejections[g.name] += 1
        if self.keep_decisions and len(self.sample_decisions) < self.keep_decisions and d.stage1_passed:
            self.sample_decisions.append(d.to_dict(include_features=False))

    def execution(self, intent: Any, rep: Any) -> None:
        self.executions["total"] += 1
        self.executions[rep.status.value] += 1
        if not rep.filled:
            self.executions["failed"] += 1

    def position(self, pos: Any, event: str, detail: dict | None = None) -> None: ...

    def trade(self, ct: ClosedTrade) -> None:
        self.trades.append(ct)

    def risk_event(self, kind: str, detail: dict) -> None:
        self.rejections[f"risk_event:{kind}"] += 1

    def outcome(self, s: OutcomeSample) -> None:
        self.outcomes.append(s)


class FrozenOutcomeStore(OutcomeStore):
    """Walk-forward test folds use the training outcomes only; new outcomes are recorded but not learned."""

    def add(self, s: OutcomeSample) -> None:
        return


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    outcomes: list[OutcomeSample]
    equity_curve: list[tuple[float, float]]
    decisions: dict
    rejections: dict
    executions: dict
    n_events: int
    n_tokens: int
    start: float | None
    end: float | None
    configuration_version: str
    synthetic: bool
    killed: dict | None = None
    sample_decisions: list[dict] = field(default_factory=list)
    labels: list[dict] = field(default_factory=list)

    def trade_dicts(self) -> list[dict]:
        return [t.to_dict() for t in self.trades]


class Backtester:
    def __init__(
        self,
        cfg: AppConfig,
        outcomes: OutcomeStore | None = None,
        seed: int = 0,
        calibration: bool = True,
        trade_start: float | None = None,
        allow_synthetic: bool | None = None,
        keep_decisions: int = 0,
        label_outcomes: bool = False,
        research_only: bool = False,
    ) -> None:
        self.cfg = cfg
        self.outcomes = outcomes if outcomes is not None else OutcomeStore()
        self.seed, self.calibration, self.trade_start = seed, calibration, trade_start
        self.allow_synthetic = cfg.backtest.allow_synthetic if allow_synthetic is None else allow_synthetic
        self.keep_decisions = keep_decisions
        self.label_outcomes, self.research_only = label_outcomes, research_only

    def run(self, events: Iterable[MarketEvent], end: float | None = None) -> BacktestResult:
        cfg = self.cfg
        it = iter(events)
        first = next(it, None)
        sink = CollectingSink(keep_decisions=self.keep_decisions)
        if first is None:
            return BacktestResult([], [], [], {}, {}, {}, 0, 0, None, None, cfg.configuration_version, False)
        clock = ManualClock(first.t)
        core = build_core(cfg, clock, Mode.PAPER, sink=sink, outcomes=self.outcomes)
        core.outcome_source = "backtest"
        core.calibration_enabled = self.calibration
        labeler = None
        if self.label_outcomes:
            from tradingagent.research.labeling import OutcomeLabeler

            labeler = OutcomeLabeler(
                core.market,
                cfg.research.label_horizons_s,
                cfg.research.label_notional_sol,
                core.decisions.versions["feature_version"],
            )
        fills = PaperFillModel(cfg.execution, seed=self.seed)
        prio = cfg.execution.priority_fee.static_micro_lamports
        pending: list[tuple[float, int, str, tuple]] = []
        seq = itertools.count()
        inflight: set[str] = set()
        interval = cfg.backtest.evaluation_interval_s
        next_tick = first.t
        last_sweep = first.t
        equity: list[tuple[float, float]] = []
        last_eq_t = -1e18
        n = 0
        synthetic = False
        trade_start = self.trade_start or -1e18

        def busy(mint: str) -> bool:
            return mint in inflight

        def run_pending(until: float) -> None:
            while pending and pending[0][0] <= until:
                due, _, kind, payload = heapq.heappop(pending)
                clock.advance_to(max(due, clock.now()))
                now = clock.now()
                if kind == "entry":
                    d, intent = payload
                    tm = core.market.market(intent.mint)
                    rep = fills.fill(intent, tm.adapter(core.schedule) if tm else None, now)
                    core.on_entry_report(d, intent, rep, now)
                    inflight.discard(intent.mint)
                else:
                    pos, sig, intent = payload
                    tm = core.market.market(intent.mint)
                    rep = fills.fill(intent, tm.adapter(core.schedule) if tm else None, now)
                    core.on_exit_report(pos, sig, intent, rep, now)
                    inflight.discard(intent.mint)

        def tick(now: float) -> None:
            nonlocal last_sweep, last_eq_t
            core.refresh_regime(now)
            trading = now >= trade_start
            for mint in core.due_candidates(now):
                if not trading:
                    continue
                d = core.stage1(mint, now, prio)
                if d.stage1_passed and not self.research_only:
                    d = core.stage2(d, AIResult(status="DISABLED"), now, prio, busy(mint))
                core.finish_decision(d, now)
                if labeler is not None:
                    labeler.on_decision(d)
                if d.outcome is DecisionOutcome.APPROVED and d.intent is not None and not busy(mint):
                    inflight.add(mint)
                    heapq.heappush(pending, (now + cfg.execution.assumed_latency_s, next(seq), "entry", (d, d.intent)))
            for pos, sig, intent in core.exit_intents(now, prio, busy):
                inflight.add(pos.mint)
                heapq.heappush(pending, (now + cfg.execution.assumed_latency_s, next(seq), "exit", (pos, sig, intent)))
            if core.virtual:
                core.calibration_step(now)
            if labeler is not None and labeler.pending:
                labeler.step(now)
            core.periodic_risk(now)
            if now - last_sweep >= 60:
                prot = {p.mint for p in core.portfolio.positions.values()} | set(core.virtual)
                if labeler is not None:
                    prot |= labeler.protected_mints
                core.market.sweep(now, protected=prot)
                last_sweep = now
            if now - last_eq_t >= 60:
                equity.append((now, core.portfolio.equity / 1e9))
                last_eq_t = now

        def advance(to: float) -> None:
            nonlocal next_tick
            while next_tick <= to:
                run_pending(next_tick)
                clock.advance_to(max(next_tick, clock.now()))
                tick(next_tick)
                next_tick += interval
                idle = (
                    not core._due
                    and not core.portfolio.positions
                    and not core.virtual
                    and not pending
                    and not (labeler is not None and labeler.pending)
                )
                if idle and next_tick < to:
                    next_tick = to - ((to - next_tick) % interval)  # skip empty stretches
            run_pending(to)

        for ev in itertools.chain([first], it):
            if ev.source == "synthetic":
                synthetic = True
                if not self.allow_synthetic:
                    raise SyntheticDataRefused(
                        "synthetic events in backtest input; pass allow_synthetic=True "
                        "(results say nothing about real edge)"
                    )
            if end is not None and ev.t > end:
                break
            advance(ev.t)
            clock.advance_to(max(ev.t, clock.now()))
            core.ingest(ev)
            n += 1
        final_t = clock.now() + 1.0
        advance(final_t)
        self._close_all(core, fills, final_t, prio)
        if core.virtual:
            for vp in list(core.virtual.values()):
                vp.pos.opened_at = min(vp.pos.opened_at, final_t)
            # unresolved calibration positions are dropped: their outcome is unknown at end of data
            core.virtual.clear()
        equity.append((clock.now(), core.portfolio.equity / 1e9))
        return BacktestResult(
            trades=sink.trades,
            outcomes=sink.outcomes,
            equity_curve=equity,
            decisions=dict(sink.decisions),
            rejections=dict(sink.rejections),
            executions=dict(sink.executions),
            n_events=n,
            n_tokens=len(core.market.registry.tokens),
            start=first.t,
            end=clock.now(),
            configuration_version=cfg.configuration_version,
            synthetic=synthetic,
            killed=core.kill.state.to_dict() if core.kill.state.killed else None,
            sample_decisions=sink.sample_decisions,
            labels=labeler.completed if labeler is not None else [],
        )

    @staticmethod
    def _close_all(core: TradingCore, fills: PaperFillModel, now: float, prio: int) -> None:
        for pos in list(core.portfolio.positions.values()):
            sig = ExitSignal(ExitReason.END_OF_DATA, 1.0, "end of backtest data")
            intent = core.build_exit_intent(pos, sig, now, prio)
            tm = core.market.market(pos.mint)
            if intent is None:
                ct = core.portfolio.write_off(pos, now, ExitReason.END_OF_DATA.value)
                core.sink.trade(ct)
                continue
            rep = fills.fill(intent, tm.adapter(core.schedule) if tm else None, now)
            if not rep.filled:
                ct = core.portfolio.write_off(pos, now, ExitReason.END_OF_DATA.value)
                core.sink.trade(ct)
            else:
                core.on_exit_report(pos, sig, intent, rep, now)
