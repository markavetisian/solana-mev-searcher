"""Live runtime for RESEARCH / PAPER / SHADOW / LIVE. Same TradingCore as the backtester; only the provider differs.

Tasks:
  ingest      — event source -> core.ingest -> (optional) persist raw events
  decide      — every evaluation tick: stage1 -> AI (bounded, concurrent) -> stage2 -> order (persisted first)
  manage      — exit checks, calibration positions, periodic risk, sweeps
  publish     — runtime state for the API (status, portfolio, opportunities), heartbeat
  commands    — operator commands from the API (pause / resume / kill / reset / paper reset)
  health      — RPC health, wallet balance sanity, data staleness
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any

from tradingagent.ai.analyst import AIResult, GuardedAnalyst
from tradingagent.alerts.telegram import AlertSink, LogAlertSink, fmt_closed, fmt_halt, fmt_opened, fmt_opportunity
from tradingagent.common.clock import SystemClock
from tradingagent.common.config import AppConfig, LiveLimits
from tradingagent.common.logging import get_logger
from tradingagent.common.metrics import METRICS, Timer
from tradingagent.common.types import DecisionOutcome, ExecStatus, LifecycleState, Mode
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.execution.orders import ExecutionReport, OrderIntent
from tradingagent.execution.providers import ExecutionProvider
from tradingagent.ingestion.sources import EventSource
from tradingagent.portfolio.portfolio import Position
from tradingagent.risk.killswitch import RESET_PHRASE, KillState
from tradingagent.solana.priority import PriorityFeeOracle
from tradingagent.solana.rpc import RpcError, RpcPool
from tradingagent.solana.wallet import BalanceGuard
from tradingagent.storage import models as m
from tradingagent.storage.db import BatchWriter, Database, DatabaseUnavailable
from tradingagent.storage.sink import DbSink, execution_row
from tradingagent.strategy.core import MultiSink, TradingCore, build_core
from tradingagent.strategy.decision import Decision
from tradingagent.strategy.ev import OutcomeSample, OutcomeStore
from tradingagent.strategy.exits import ExitSignal

log = get_logger("runtime")


class TradingRuntime:
    def __init__(
        self,
        cfg: AppConfig,
        mode: Mode,
        source: EventSource,
        provider: ExecutionProvider | None,
        db: Database | None,
        analyst: GuardedAnalyst,
        alerts: AlertSink | None = None,
        rpc: RpcPool | None = None,
        live_limits: LiveLimits | None = None,
        wallet_pubkey: str | None = None,
        persist_events: bool = True,
    ) -> None:
        self.cfg, self.mode, self.source, self.provider = cfg, mode, source, provider
        self.db, self.analyst, self.alerts = db, analyst, alerts or LogAlertSink()
        self.rpc, self.live_limits, self.wallet_pubkey = rpc, live_limits, wallet_pubkey
        self.persist_events = persist_events and db is not None
        self.clock = SystemClock()
        self.session_id = "sess_" + uuid.uuid4().hex[:12]
        self.writer: BatchWriter | None = None
        self.dbsink: DbSink | None = None
        self.core: TradingCore | None = None
        self.priority = PriorityFeeOracle(cfg.execution.priority_fee, rpc)
        self.balance_guard = BalanceGuard(
            int(cfg.killswitch.balance_mismatch_tolerance_sol * LAMPORTS_PER_SOL),
            int(cfg.risk.min_wallet_balance_sol * LAMPORTS_PER_SOL),
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._inflight: dict[str, asyncio.Task] = {}
        self.started_at = time.time()
        self._alerted: dict[str, float] = {}
        self.labeler = None
        self._last_token_flush = 0.0

    # ---- setup ----------------------------------------------------------------------------------------------
    async def _setup(self) -> None:
        outcomes = OutcomeStore()
        saved_kill: dict | None = None
        if self.db is not None:
            sess, _ = await self.db.get_state(f"session:{self.mode.value}")
            if sess and sess.get("session_id"):
                self.session_id = sess["session_id"]
            else:
                await self.db.set_state(f"session:{self.mode.value}", {"session_id": self.session_id, "t": time.time()})
            saved_kill, _ = await self.db.get_state("killswitch")
            await self._load_outcomes(outcomes)
            self.writer = BatchWriter(
                self.db,
                self.cfg.storage.batch_size,
                self.cfg.storage.flush_interval_s,
                self.cfg.storage.max_queue,
                on_error=self._on_db_error,
                on_ok=self._on_db_ok,
            )
            self.dbsink = DbSink(self.writer, self.mode, self.session_id, self.cfg.storage.persist_features)
            await self._record_versions()
        self.core = build_core(
            self.cfg,
            self.clock,
            self.mode,
            sink=MultiSink(self.dbsink, self),
            outcomes=outcomes,
            live_limits=self.live_limits,
            ai_model_name=self.cfg.ai.model if self.cfg.ai.enabled else None,
        )
        self.core.kill.on_change = self._on_kill_change
        if saved_kill:
            self.core.kill.restore(saved_kill)
            if self.core.kill.state.killed:
                log.critical("starting_with_kill_switch_latched", reason=self.core.kill.state.reason)
        if self.mode is Mode.RESEARCH:
            from tradingagent.research.labeling import OutcomeLabeler

            self.labeler = OutcomeLabeler(
                self.core.market,
                self.cfg.research.label_horizons_s,
                self.cfg.research.label_notional_sol,
                self.core.decisions.versions["feature_version"],
            )
        if self.dbsink is not None:
            self.core.market.registry.on_transition = lambda rec, o, n, r, t: self.dbsink.transition(
                rec, o.value, n.value, r, t
            )
        log.info(
            "runtime_ready",
            mode=self.mode.value,
            session_id=self.session_id,
            configuration_version=self.cfg.configuration_version,
            outcomes_loaded=len(outcomes),
        )

    async def _load_outcomes(self, store: OutcomeStore) -> None:
        from sqlalchemy import select

        assert self.db is not None
        cutoff = time.time() - self.cfg.ev.outcome_lookback_days * 86_400
        async with self.db.session() as s:
            rows = (
                (
                    await s.execute(
                        select(m.OutcomeSampleRow).where(m.OutcomeSampleRow.t >= cutoff).order_by(m.OutcomeSampleRow.t)
                    )
                )
                .scalars()
                .all()
            )
        for r in rows:
            store.add(
                OutcomeSample(
                    t=r.t,
                    score=r.score,
                    regime=r.regime,
                    gross_return=r.gross_return,
                    net_return=r.net_return,
                    hold_s=r.hold_s,
                    source=r.source,
                    strategy_version=r.strategy_version,
                )
            )

    async def _record_versions(self) -> None:
        assert self.db is not None
        from tradingagent.common import versions as v

        with contextlib.suppress(DatabaseUnavailable):
            await self.db.write_now(
                m.ConfigurationVersion.__table__,
                {"version": self.cfg.configuration_version, "config": self.cfg.model_dump(mode="json")},
                "version",
            )
            await self.db.write_now(
                m.StrategyVersion.__table__,
                {
                    "version": v.STRATEGY_VERSION,
                    "components": {
                        "feature": v.FEATURE_VERSION,
                        "scoring": v.SCORING_VERSION,
                        "ev": v.EV_MODEL_VERSION,
                        "filters": v.FILTER_VERSION,
                        "idl": v.PUMP_IDL_COMMIT,
                    },
                },
                "version",
            )

    # ---- sink callbacks (alerts) -------------------------------------------------------------------------------
    def decision(self, d: Decision) -> None:
        if d.score and d.score.total >= self.cfg.alerts.min_score_alert and d.stage1_passed:
            last = self._alerted.get(d.mint, 0)
            if time.time() - last > 600:
                self._alerted[d.mint] = time.time()
                self.alerts.send(fmt_opportunity(d, self.mode.value))

    def execution(self, intent: OrderIntent, rep: ExecutionReport) -> None: ...

    def position(self, pos: Position, event: str, detail: dict | None = None) -> None:
        if event == "OPENED":
            self.alerts.send(fmt_opened(pos, self.mode.value))

    def trade(self, ct: Any) -> None:
        self.alerts.send(fmt_closed(ct, self.mode.value))

    def risk_event(self, kind: str, detail: dict) -> None: ...

    def outcome(self, s: OutcomeSample) -> None: ...

    def _on_kill_change(self, state: KillState, action: str) -> None:
        if action == "KILL":
            self.alerts.send(fmt_halt(state.reason or "", self.mode.value), "CRITICAL")
            if self.dbsink:
                self.dbsink.risk_event("KILL_SWITCH", state.to_dict(), "CRITICAL")
        if self.db is not None:
            asyncio.get_event_loop().create_task(self._persist_kill(state))

    async def _persist_kill(self, state: KillState) -> None:
        try:
            await self.db.set_state("killswitch", state.to_dict())  # type: ignore[union-attr]
        except DatabaseUnavailable as e:
            log.critical("kill_state_persist_failed", error=str(e))

    def _on_db_error(self, err: str) -> None:
        if self.core:
            self.core.kill.record_db_error(err)

    def _on_db_ok(self) -> None:
        if self.core:
            self.core.kill.record_db_ok()

    # ---- loops ---------------------------------------------------------------------------------------------------
    async def _ingest_loop(self) -> None:
        assert self.core is not None
        async for ev in self.source.events():
            if self._stop.is_set():
                break
            t0 = time.perf_counter()
            self.core.ingest(ev)
            if self.persist_events and self.dbsink:
                self.dbsink.market_event(ev)
            METRICS.observe_ms("ingest.process", (time.perf_counter() - t0) * 1000)
            METRICS.observe_ms("ingest.lag", max(0.0, (time.time() - ev.observed_at) * 1000))
            METRICS.inc("ingest.events")

    def _busy(self, mint: str) -> bool:
        return mint in self._inflight or (self.provider is not None and self.provider.busy(mint))

    async def _decide_loop(self) -> None:
        core = self.core
        assert core is not None
        while not self._stop.is_set():
            await asyncio.sleep(self.cfg.backtest.evaluation_interval_s)
            now = self.clock.now()
            core.refresh_regime(
                now,
                rpc_healthy=self.rpc.healthy if self.rpc else True,
                feed_connected=getattr(self.source, "connected", True),
            )
            prio = self.priority.current()
            mints = core.due_candidates(now)
            stage1: list[Decision] = []
            with Timer(METRICS, "strategy.tick"):
                for mint in mints:
                    d = core.stage1(mint, now, prio)
                    if self.labeler is not None:
                        self.labeler.on_decision(d)
                    if d.stage1_passed:
                        stage1.append(d)
                    else:
                        core.finish_decision(d, now)
            if not stage1:
                continue
            if self.mode is Mode.RESEARCH:
                for d in stage1:
                    core.finish_decision(d, now)
                continue
            ai_results = await asyncio.gather(*(self._ai(d) for d in stage1))
            for d, ai in zip(stage1, ai_results, strict=True):
                now2 = self.clock.now()
                d = core.stage2(d, ai, now2, prio, self._busy(d.mint))
                core.finish_decision(d, now2)
                if d.outcome is DecisionOutcome.APPROVED and d.intent and self.provider and not self._busy(d.mint):
                    await self._submit_entry(d, d.intent)

    async def _ai(self, d: Decision) -> AIResult:
        if not self.cfg.ai.enabled:
            return AIResult(status="DISABLED")
        assert self.core is not None
        return await self.analyst.analyze(self.core.decisions.ai_payload(d))

    async def _persist_order(self, intent: OrderIntent, versions: dict) -> bool:
        """Orders are durable BEFORE execution. If the DB is down we fail closed (no order)."""
        if self.db is None or self.dbsink is None:
            return self.mode is not Mode.LIVE  # LIVE without a database is never allowed
        try:
            await self.db.write_now(m.Order.__table__, self.dbsink.order_row(intent, versions))
            return True
        except DatabaseUnavailable as e:
            assert self.core is not None
            self.core.kill.record_db_error(str(e))
            log.error("order_not_persisted_fail_closed", execution_id=intent.execution_id, error=str(e)[:200])
            return False

    async def _submit_entry(self, d: Decision, intent: OrderIntent) -> None:
        if not await self._persist_order(intent, d.versions):
            return
        self._inflight[intent.mint] = asyncio.create_task(self._run_entry(d, intent))

    async def _run_entry(self, d: Decision, intent: OrderIntent) -> None:
        assert self.core is not None and self.provider is not None
        try:
            with Timer(METRICS, "execution.entry"):
                rep = await self.provider.execute(intent, self._adapter)
            await self._persist_execution(rep)
            self.core.on_entry_report(d, intent, rep, self.clock.now())
        except Exception as e:
            log.error("entry_execution_crashed", execution_id=intent.execution_id, exc_info=True, error=str(e))
            self.core.kill.record_execution_failure(f"crash: {type(e).__name__}")
        finally:
            self._inflight.pop(intent.mint, None)

    async def _persist_execution(self, rep: ExecutionReport) -> None:
        if self.db is None:
            return
        try:
            await self.db.write_now(m.Execution.__table__, execution_row(rep), upsert_key="execution_id")
        except DatabaseUnavailable as e:
            assert self.core is not None
            self.core.kill.record_db_error(f"execution not persisted: {e}")

    def _adapter(self, mint: str):
        assert self.core is not None
        tm = self.core.market.market(mint)
        return tm.adapter(self.core.schedule) if tm else None

    async def _manage_loop(self) -> None:
        core = self.core
        assert core is not None
        last_sweep = last_snap = 0.0
        emergency_done = False
        while not self._stop.is_set():
            await asyncio.sleep(self.cfg.strategy.exit_check_interval_s)
            now = self.clock.now()
            prio = self.priority.current()
            if self.provider is not None:
                if core.kill.state.killed:
                    if self.cfg.killswitch.emergency_exit_on_kill and not emergency_done:
                        emergency_done = True
                        for pos, sig, intent in core.emergency_exit_intents(now, prio):
                            await self._submit_exit(pos, sig, intent)
                else:
                    emergency_done = False
                    for pos, sig, intent in core.exit_intents(now, prio, self._busy):
                        await self._submit_exit(pos, sig, intent)
            if core.virtual:
                core.calibration_step(now)
            if self.labeler is not None:
                for row in self.labeler.step(now):
                    if self.writer:
                        self.writer.put(
                            m.SignalOutcome.__table__,
                            {
                                "decision_id": row["decision_id"],
                                "mint": row["mint"],
                                "t": row["t"],
                                "score": row["score"],
                                "regime": row["regime"],
                                "features": row["features"],
                                "labels": row["labels"],
                                "feature_version": row["feature_version"],
                            },
                            ignore_conflicts=True,
                        )
            core.periodic_risk(now)
            if now - last_sweep > 60:
                protected = {p.mint for p in core.portfolio.positions.values()} | set(core.virtual)
                if self.labeler is not None:
                    protected |= self.labeler.protected_mints
                core.market.sweep(now, protected)
                last_sweep = now
            if (
                now - last_snap > self.cfg.tracking.snapshot_interval_s
                and self.dbsink
                and self.cfg.storage.persist_snapshots
            ):
                last_snap = now
                for tm in core.market.tokens.values():
                    if (
                        tm.record.state
                        in (LifecycleState.BONDING_CURVE, LifecycleState.PUMPSWAP, LifecycleState.ACTIVE)
                        and now - tm.record.last_trade_at < 60
                    ):
                        self.dbsink.snapshot(core.market.snapshot(tm, now))
                self.dbsink.portfolio_snapshot(core.portfolio.snapshot(now))
                await self._upsert_tokens(now)

    async def _upsert_tokens(self, now: float) -> None:
        assert self.core is not None and self.db is not None
        rows = []
        for tm in self.core.market.tokens.values():
            r = tm.record
            if r.last_seen_at < self._last_token_flush:
                continue
            d = r.to_dict()
            rows.append(
                {
                    "mint": r.mint,
                    "name": d["name"],
                    "symbol": d["symbol"],
                    "uri": d["uri"],
                    "creator": d["creator"],
                    "created_at": d["created_at"],
                    "market_state": d["market_state"],
                    "bonding_curve_address": d["bonding_curve_address"],
                    "pool_address": d["pool_address"],
                    "graduation_state": d["graduation_state"],
                    "first_seen_slot": d["first_seen_slot"],
                    "last_seen_slot": d["last_seen_slot"],
                    "first_seen_at": d["first_seen_at"],
                    "last_trade_at": d["last_trade_at"],
                    "partial_history": d["partial_history"],
                    "token_program": d["token_program"],
                    "quote_mint": d["quote_mint"],
                    "is_mayhem": d["is_mayhem"],
                    "untradeable_reason": d["untradeable_reason"],
                }
            )
        self._last_token_flush = now
        try:
            await self.db.upsert_many(m.Token.__table__, rows, "mint")
        except DatabaseUnavailable as e:
            self._on_db_error(str(e))

    async def _submit_exit(self, pos: Position, sig: ExitSignal, intent: OrderIntent) -> None:
        if not await self._persist_order(intent, pos.versions):
            pos.pending_exit_id, pos.status = None, "OPEN"
            return
        self._inflight[pos.mint] = asyncio.create_task(self._run_exit(pos, sig, intent))

    async def _run_exit(self, pos: Position, sig: ExitSignal, intent: OrderIntent) -> None:
        assert self.core is not None and self.provider is not None
        try:
            with Timer(METRICS, "execution.exit"):
                rep = await self.provider.execute(intent, self._adapter)
            await self._persist_execution(rep)
            self.core.on_exit_report(pos, sig, intent, rep, self.clock.now())
        except Exception as e:
            log.error("exit_execution_crashed", execution_id=intent.execution_id, exc_info=True, error=str(e))
            pos.pending_exit_id, pos.status = None, "OPEN"
            self.core.kill.record_execution_failure(f"crash: {type(e).__name__}")
        finally:
            self._inflight.pop(pos.mint, None)

    # ---- publishing for the API ---------------------------------------------------------------------------------
    def status(self) -> dict:
        core = self.core
        assert core is not None
        now = self.clock.now()
        stats = core.market.activity.stats(now)
        return {
            "mode": self.mode.value,
            "session_id": self.session_id,
            "heartbeat": now,
            "started_at": self.started_at,
            "configuration_version": self.cfg.configuration_version,
            "versions": core.decisions.versions,
            "kill_switch": core.kill.state.to_dict(),
            "regime": core.regime.current.to_dict(),
            "market": stats,
            "tracked_tokens": len(core.market.tokens),
            "feed": {
                "connected": getattr(self.source, "connected", None),
                "staleness_s": getattr(self.source, "staleness_s", None),
            },
            "rpc": self.rpc.health() if self.rpc else None,
            "ai": {"enabled": self.cfg.ai.enabled, "model": self.cfg.ai.model if self.cfg.ai.enabled else None},
            "calibration_positions": len(core.virtual),
            "outcome_samples": len(core.outcomes),
            "storage_backlog": self.writer.backlog if self.writer else None,
            "inflight_orders": list(self._inflight),
            "wallet": self.wallet_pubkey,
        }

    def opportunities(self, limit: int = 50) -> list[dict]:
        core = self.core
        assert core is not None
        rows = []
        for d in core.last_decisions.values():
            if d.score is None or d.features is None:
                continue
            f = d.features
            rows.append(
                {
                    "mint": d.mint,
                    "symbol": d.symbol,
                    "t": d.t,
                    "score": round(d.score.total, 1),
                    "outcome": d.outcome.value,
                    "market_state": d.market_state,
                    "market_cap_sol": f.get("mcap_sol"),
                    "liquidity_sol": f.get("liquidity_sol"),
                    "volume_sol_5m": f.get("vol_sol_300s"),
                    "buy_sell_imbalance_60s": f.get("imbalance_60s"),
                    "holder_growth_60s": f.get("holder_growth_60s"),
                    "risk": "PASS" if (d.gate("RISK_FILTER") and d.gate("RISK_FILTER").status == "PASS") else "FAIL",
                    "risk_reasons": d.gate("RISK_FILTER").reasons[:3] if d.gate("RISK_FILTER") else [],
                    "expected_value": (d.ev.ev_conservative if d.ev and d.ev.status == "OK" else None),
                    "ev_status": d.ev.status if d.ev else None,
                    "proposed_size_sol": d.sizing.size_sol if d.sizing else None,
                    "top_rejection": d.rejection_reasons[0] if d.rejection_reasons else None,
                    "decision_id": d.decision_id,
                }
            )
        rows.sort(key=lambda r: -r["score"])
        for i, r in enumerate(rows[:limit]):
            r["rank"] = i + 1
        return rows[:limit]

    def portfolio_state(self) -> dict:
        core = self.core
        assert core is not None
        now = self.clock.now()
        return {
            "snapshot": core.portfolio.snapshot(now),
            "positions": [p.to_dict() for p in core.portfolio.positions.values()],
            "recent_trades": [t.to_dict() for t in list(core.portfolio.closed)[-50:]],
        }

    async def _publish_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            if self.db is None:
                continue
            try:
                await self.db.set_state("status", self.status())
                await self.db.set_state(f"portfolio:{self.mode.value}", self.portfolio_state())
                await self.db.set_state("opportunities", {"rows": self.opportunities()})
                await self.db.set_state("metrics", METRICS.snapshot())
            except DatabaseUnavailable as e:
                self._on_db_error(str(e))

    # ---- operator commands -----------------------------------------------------------------------------------------
    async def _command_loop(self) -> None:
        from sqlalchemy import select, update

        if self.db is None:
            return
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            try:
                async with self.db.session() as s:
                    rows = (
                        (
                            await s.execute(
                                select(m.OperatorCommand)
                                .where(m.OperatorCommand.status == "PENDING")
                                .order_by(m.OperatorCommand.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                for cmd in rows:
                    result = self.apply_command(cmd.command, cmd.args or {}, cmd.operator)
                    async with self.db.engine.begin() as conn:
                        await conn.execute(
                            update(m.OperatorCommand)
                            .where(m.OperatorCommand.id == cmd.id)
                            .values(
                                status="DONE" if result.get("ok") else "REJECTED",
                                processed_at=time.time(),
                                result=result,
                            )
                        )
                    await self.db.write_now(
                        m.OperatorCommandLog.__table__,
                        {
                            "t": time.time(),
                            "command_id": cmd.id,
                            "command": cmd.command,
                            "operator": cmd.operator,
                            "status": "DONE" if result.get("ok") else "REJECTED",
                            "detail": result,
                        },
                    )
            except (DatabaseUnavailable, Exception) as e:
                log.error("command_loop_error", error=str(e)[:200])

    def apply_command(self, command: str, args: dict, operator: str) -> dict:
        core = self.core
        assert core is not None
        if command == "pause":
            core.kill.pause(args.get("reason", "operator pause"), operator)
        elif command == "resume":
            if core.kill.state.killed:
                if not core.kill.reset(operator, args.get("confirm", "")):
                    return {"ok": False, "error": f"kill switch latched; resume requires confirm={RESET_PHRASE}"}
            core.kill.resume(operator)
        elif command == "kill":
            core.kill.activate(args.get("reason", "operator kill"), source=f"operator:{operator}")
        elif command == "paper_reset":
            if self.mode is Mode.LIVE:
                return {"ok": False, "error": "paper reset is not available in LIVE mode"}
            if core.portfolio.positions:
                return {"ok": False, "error": "close open paper positions first (or kill + wait)"}
            from tradingagent.portfolio.portfolio import Portfolio

            core.portfolio = Portfolio(int(self.cfg.paper.starting_equity_sol * LAMPORTS_PER_SOL), time.time())
            core.decisions.portfolio = core.portfolio
            self.session_id = "sess_" + uuid.uuid4().hex[:12]
            if self.dbsink:
                self.dbsink.session_id = self.session_id
            if self.db:
                asyncio.get_event_loop().create_task(
                    self.db.set_state(f"session:{self.mode.value}", {"session_id": self.session_id, "t": time.time()})
                )
        else:
            return {"ok": False, "error": f"unknown command {command}"}
        log.warning("operator_command", command=command, operator=operator)
        return {"ok": True, "command": command, "kill_switch": core.kill.state.to_dict(), "session_id": self.session_id}

    # ---- health ------------------------------------------------------------------------------------------------------
    async def _health_loop(self) -> None:
        core = self.core
        assert core is not None
        while not self._stop.is_set():
            await asyncio.sleep(5.0)
            if self.rpc is not None:
                core.kill.observe_rpc(self.rpc.healthy)
                if self.wallet_pubkey and self.mode in (Mode.LIVE, Mode.SHADOW):
                    try:
                        bal = await self.rpc.get_balance(self.wallet_pubkey)
                        METRICS.set("wallet.balance_sol", bal / LAMPORTS_PER_SOL)
                        if self.mode is Mode.LIVE:
                            problem = self.balance_guard.check(bal, core.portfolio.cash, bool(self._inflight))
                            if problem:
                                core.kill.mismatch(problem)
                    except RpcError:
                        pass
                with contextlib.suppress(RpcError):
                    await self.priority.refresh(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
            if self.db is not None and not await self.db.ping():
                core.kill.record_db_error("ping failed")

    # ---- lifecycle ---------------------------------------------------------------------------------------
    async def run(self) -> None:
        await self._setup()
        if self.writer:
            self.writer.start()
        if hasattr(self.alerts, "start"):
            self.alerts.start()  # type: ignore[attr-defined]
        loops = [
            self._ingest_loop,
            self._decide_loop,
            self._manage_loop,
            self._publish_loop,
            self._command_loop,
            self._health_loop,
        ]
        self._tasks = [asyncio.create_task(fn(), name=fn.__name__) for fn in loops]
        pending = set(self._tasks)
        while pending and not self._stop.is_set():
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            failed = [t for t in done if not t.cancelled() and t.exception() is not None]
            for t in failed:
                log.critical("runtime_task_failed", task=t.get_name(), error=repr(t.exception()))
                if self.core is not None:
                    self.core.kill.activate(f"runtime task {t.get_name()} crashed: {t.exception()!r}")
            if failed:
                break
            if any(t.get_name() == "_ingest_loop" for t in done):
                log.warning("event_source_exhausted", detail="no more events; positions still managed until stop")
        await self.shutdown()

    async def shutdown(self) -> None:
        self._stop.set()
        if hasattr(self.source, "stop"):
            self.source.stop()  # type: ignore[attr-defined]
        for t in list(self._inflight.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=self.cfg.execution.confirm_timeout_s + 30)
        for t in self._tasks:
            t.cancel()
        if self.writer:
            await self.writer.stop()
        if self.db is not None and self.core is not None:
            with contextlib.suppress(DatabaseUnavailable):
                await self.db.set_state(f"portfolio:{self.mode.value}", self.portfolio_state())
                await self.db.set_state("killswitch", self.core.kill.state.to_dict())
        log.info("runtime_stopped", mode=self.mode.value)


def provider_status_ok(rep: ExecutionReport) -> bool:
    return rep.status in (ExecStatus.CONFIRMED, ExecStatus.FILLED_PAPER, ExecStatus.FILLED_SHADOW)
