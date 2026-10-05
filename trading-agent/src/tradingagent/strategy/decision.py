"""Trade decision pipeline. Deterministic gates have sole authority; the AI can only veto.

    DISCOVER -> VALIDATE DATA -> RISK FILTER -> FEATURES -> QUANT SCORE -> REGIME CHECK -> AI ANALYSIS
             -> EXPECTED VALUE -> POSITION SIZE -> EXECUTION SIMULATION -> RISK ENGINE -> APPROVE / REJECT

Implementation notes:
  * Features are computed once up front because the risk filter is defined on raw measurements (liquidity,
    impact, concentration); the gate list is still recorded in the order above.
  * Sizing and execution simulation are computed before the EV gate is *evaluated* so that EV uses the exact
    cost of the proposed size; their gate results are recorded after EV, as above.
  * Stage 1 (data/filter/score/regime) is synchronous and cheap. The AI is only called for candidates that pass
    stage 1. Stage 2 consumes an AIResult (possibly DISABLED/TIMEOUT/MALFORMED) and finishes synchronously.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from tradingagent.ai.analyst import AIResult
from tradingagent.ai.prompt import PROMPT_VERSION, build_payload
from tradingagent.common.config import AppConfig, LiveLimits
from tradingagent.common.metrics import METRICS
from tradingagent.common.types import TRADEABLE_STATES, DecisionOutcome, Side
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.common.versions import (
    EV_MODEL_VERSION,
    FEATURE_VERSION,
    FILTER_VERSION,
    SCORING_VERSION,
    STRATEGY_VERSION,
)
from tradingagent.execution.orders import OrderIntent, new_execution_id
from tradingagent.execution.simulator import ExecutionEstimate, ExecutionSimulator
from tradingagent.features.engine import FeatureEngine, FeatureVector
from tradingagent.market.engine import MarketDataEngine
from tradingagent.portfolio.portfolio import Portfolio
from tradingagent.risk.engine import RiskEngine
from tradingagent.risk.filters import TokenRiskFilter
from tradingagent.risk.sizing import PositionSizer, SizingInputs, SizingResult
from tradingagent.scoring.model import ScoreCard, Scorer
from tradingagent.strategy.ev import EVEstimate, EVModel
from tradingagent.strategy.regime import RegimeDetector, RegimeState

GATES = (
    "VALIDATE_DATA",
    "RISK_FILTER",
    "FEATURES",
    "QUANT_SCORE",
    "REGIME",
    "AI_ANALYSIS",
    "EXPECTED_VALUE",
    "POSITION_SIZE",
    "EXECUTION_SIMULATION",
    "RISK_ENGINE",
)


@dataclass
class Gate:
    name: str
    status: str  # PASS | FAIL | SKIPPED | INFO
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "reasons": self.reasons, "detail": self.detail}


@dataclass
class Decision:
    decision_id: str
    mint: str
    t: float
    outcome: DecisionOutcome = DecisionOutcome.REJECTED
    gates: list[Gate] = field(default_factory=list)
    features: FeatureVector | None = None
    score: ScoreCard | None = None
    regime: RegimeState | None = None
    ai: AIResult | None = None
    ev: EVEstimate | None = None
    sizing: SizingResult | None = None
    execution: ExecutionEstimate | None = None
    intent: OrderIntent | None = None
    versions: dict[str, str] = field(default_factory=dict)
    symbol: str | None = None
    market_state: str | None = None
    calibration_candidate: bool = False
    stage1_passed: bool = False

    def gate(self, name: str) -> Gate | None:
        return next((g for g in self.gates if g.name == name), None)

    @property
    def rejection_reasons(self) -> list[str]:
        return [f"{g.name}: {r}" for g in self.gates if g.status == "FAIL" for r in g.reasons]

    def render(self) -> str:
        head = f"{self.outcome.value}  Token: {self.symbol or self.mint[:8]}"
        if self.outcome is DecisionOutcome.APPROVED and self.sizing:
            return head + f"  size={self.sizing.size_sol:.4f} SOL  score={self.score.total if self.score else 0:.0f}"
        return head + "\n" + "\n".join(self.rejection_reasons or ["no edge"])

    def to_dict(self, include_features: bool = True) -> dict:
        return {
            "decision_id": self.decision_id,
            "mint": self.mint,
            "symbol": self.symbol,
            "t": self.t,
            "outcome": self.outcome.value,
            "market_state": self.market_state,
            "gates": [g.to_dict() for g in self.gates],
            "rejection_reasons": self.rejection_reasons,
            "score": self.score.to_dict() if self.score else None,
            "regime": self.regime.regime.value if self.regime else None,
            "ai": self.ai.to_dict() if self.ai else None,
            "ev": self.ev.to_dict() if self.ev else None,
            "sizing": self.sizing.to_dict() if self.sizing else None,
            "execution": self.execution.to_dict() if self.execution else None,
            "versions": self.versions,
            "calibration_candidate": self.calibration_candidate,
            "features": (self.features.values if (self.features and include_features) else None),
        }


class DecisionEngine:
    def __init__(
        self,
        cfg: AppConfig,
        market: MarketDataEngine,
        features: FeatureEngine,
        scorer: Scorer,
        token_filter: TokenRiskFilter,
        regime: RegimeDetector,
        ev_model: EVModel,
        sizer: PositionSizer,
        simulator: ExecutionSimulator,
        risk: RiskEngine,
        portfolio: Portfolio,
        live_limits: LiveLimits | None = None,
        ai_model_name: str | None = None,
    ) -> None:
        self.cfg, self.market, self.features, self.scorer = cfg, market, features, scorer
        self.filter, self.regime, self.ev_model, self.sizer = token_filter, regime, ev_model, sizer
        self.sim, self.risk, self.portfolio, self.live_limits = simulator, risk, portfolio, live_limits
        self.versions = {
            "strategy_version": STRATEGY_VERSION,
            "feature_version": FEATURE_VERSION,
            "scoring_version": SCORING_VERSION,
            "filter_version": FILTER_VERSION,
            "configuration_version": cfg.configuration_version,
            "model_version": EV_MODEL_VERSION,
            "ai_model": ai_model_name if cfg.ai.enabled else "none",
            "ai_prompt_version": PROMPT_VERSION,
        }

    # ---- stage 1 ----------------------------------------------------------------------------------------------
    def stage1(self, mint: str, now: float, priority_micro_lamports: int) -> Decision:
        d = Decision(decision_id="dec_" + uuid.uuid4().hex[:20], mint=mint, t=now, versions=dict(self.versions))
        tm = self.market.market(mint)
        t0 = time.perf_counter()
        # VALIDATE DATA
        g = Gate("VALIDATE_DATA", "PASS")
        if tm is None:
            g.status, g.reasons = "FAIL", ["token not tracked"]
            d.gates.append(g)
            return self._finish_skipped(d, from_index=1)
        rec = tm.record
        d.symbol, d.market_state = rec.symbol, rec.state.value
        staleness = self.market.activity.staleness(now)
        g.detail = {"state": rec.state.value, "staleness_s": staleness, "reserve_mismatches": tm.reserve_mismatches}
        if rec.state not in TRADEABLE_STATES:
            g.reasons.append(f"lifecycle state {rec.state.value} not tradeable")
        if tm.adapter(self.market.schedule) is None:
            g.reasons.append("no executable venue state")
        if staleness > self.cfg.filters.max_data_staleness_s:
            g.reasons.append(f"market data stale ({staleness:.1f}s)")
        if g.reasons:
            g.status = "FAIL"
        d.gates.append(g)
        fv = self.features.compute(tm, now, staleness)
        d.features = fv
        METRICS.observe_ms("strategy.feature_latency", (time.perf_counter() - t0) * 1000)
        # RISK FILTER
        fr = self.filter.evaluate(fv, rec.state.value)
        d.gates.append(
            Gate(
                "RISK_FILTER",
                "PASS" if fr.passed else "FAIL",
                [r.render() for r in fr.rejections],
                {"rejections": [r.__dict__ for r in fr.rejections]},
            )
        )
        d.gates.append(Gate("FEATURES", "INFO", [], {"version": fv.version, "n": len(fv.values)}))
        # QUANT SCORE
        sc = self.scorer.score(fv)
        d.score = sc
        reg = self.regime.current
        d.regime = reg
        ov = self.regime.override(reg.regime)
        min_score = self.cfg.strategy.min_score + ov.min_score_add
        d.gates.append(
            Gate(
                "QUANT_SCORE",
                "PASS" if sc.total >= min_score else "FAIL",
                [] if sc.total >= min_score else [f"score {sc.total:.1f} < required {min_score:.1f}"],
                {"score": round(sc.total, 2), "required": min_score},
            )
        )
        # REGIME
        d.gates.append(
            Gate(
                "REGIME",
                "PASS" if ov.entries_enabled else "FAIL",
                [] if ov.entries_enabled else [f"entries disabled in regime {reg.regime.value}"],
                {"regime": reg.regime.value, "reasons": reg.reasons},
            )
        )
        d.stage1_passed = all(x.status in ("PASS", "INFO") for x in d.gates)
        # Calibration: the signal-level gates passed; record a hypothetical outcome regardless of EV/AI/risk.
        d.calibration_candidate = d.stage1_passed
        METRICS.observe_ms("strategy.stage1_latency", (time.perf_counter() - t0) * 1000)
        if not d.stage1_passed:
            return self._finish_skipped(d, from_index=len(d.gates) + 0)
        return d

    def ai_payload(self, d: Decision) -> dict:
        tm = self.market.market(d.mint)
        assert tm is not None and d.features is not None and d.score is not None
        recent = [
            {
                "dt_s": round(r.t - d.t, 2),
                "side": "buy" if r.side > 0 else "sell",
                "sol": round(r.sol / LAMPORTS_PER_SOL, 4),
                "trader": r.user[:6],
            }
            for r in tm.series.window(d.t, 60)
        ]
        return build_payload(
            mint=d.mint,
            market_state=tm.record.state.value,
            score=d.score.to_dict(),
            features=d.features.values,
            risk={"filter": "passed", "creator_reasons": d.features.meta.get("creator_reasons", [])},
            wallet_analysis={
                "smart_wallet_buy_share_300s": d.features.get("smart_wallet_buy_share_300s"),
                "creator_linked_holding": d.features.get("creator_linked_holding"),
            },
            recent_events=recent,
            regime=d.regime.regime.value if d.regime else "UNKNOWN",
            name=tm.record.name,
            symbol=tm.record.symbol,
            uri=tm.record.uri,
            pattern_stats=None,
            max_chars=self.cfg.ai.max_metadata_chars,
        )

    # ---- stage 2 ----------------------------------------------------------------------------------------------
    def stage2(
        self, d: Decision, ai: AIResult, now: float, priority_micro_lamports: int, provider_busy: bool = False
    ) -> Decision:
        cfg = self.cfg
        tm = self.market.market(d.mint)
        fv, sc = d.features, d.score
        assert tm is not None and fv is not None and sc is not None and d.regime is not None
        ov = self.regime.override(d.regime.regime)
        # AI ANALYSIS (veto-only)
        d.ai = ai
        g = Gate("AI_ANALYSIS", "PASS", [], ai.to_dict())
        if ai.ok and ai.assessment is not None:
            a = ai.assessment
            if cfg.ai.veto_on_negative and a.assessment == "negative" and a.confidence >= cfg.ai.veto_min_confidence:
                g.status, g.reasons = "FAIL", [f"AI veto: negative @ {a.confidence:.2f}: {'; '.join(a.risk_flags[:3])}"]
            if "metadata_prompt_injection" in a.risk_flags:
                g.status = "FAIL"
                g.reasons.append("AI flagged prompt injection in token metadata")
        elif cfg.ai.enabled and cfg.ai.required_for_entry:
            g.status, g.reasons = "FAIL", [f"AI required for entry but unavailable ({ai.status})"]
        elif ai.status != "DISABLED":
            g.status, g.reasons = "INFO", [f"AI unavailable ({ai.status}); deterministic path continues"]
        else:
            g.status = "INFO"
        d.gates.append(g)
        pf = self.portfolio
        adapter = tm.adapter(self.market.schedule)
        if adapter is None:
            d.gates.append(Gate("EXPECTED_VALUE", "FAIL", ["venue vanished"]))
            return self._finish_skipped(d, from_index=len(d.gates))
        # sizing (computed before EV is evaluated; recorded after)
        sizing = self.sizer.size(
            SizingInputs(
                equity_sol=pf.equity / LAMPORTS_PER_SOL,
                cash_sol=pf.cash / LAMPORTS_PER_SOL,
                open_exposure_sol=pf.exposure / LAMPORTS_PER_SOL,
                daily_pnl_sol=pf.daily_pnl(now) / LAMPORTS_PER_SOL,
                hard_stop_frac=cfg.strategy.hard_stop_frac,
                entry_slippage_frac=fv.get("entry_cost_frac", 0.05) or 0.05,
                exit_slippage_frac=fv.get("exit_impact_frac", 0.05) or 0.05,
                network_cost_frac=0.002,
                liquidity_sol=fv.get("liquidity_sol", 0.0) or 0.0,
                volatility_60s=fv.get("volatility_60s"),
                execution_success_rate=pf.execution_success_rate,
                score=sc.total,
                min_score=cfg.strategy.min_score,
                regime_multiplier=ov.size_multiplier,
            )
        )
        d.sizing = sizing
        est: ExecutionEstimate | None = None
        if sizing.size_sol > 0:
            est = self.sim.simulate_entry(
                adapter,
                sizing.lamports,
                priority_micro_lamports,
                (fv.get("volatility_60s") or 0.05),
                token_2022=self.market.token_program_is_2022(tm),
            )
            d.execution = est
        # EXPECTED VALUE
        if est is not None and est.ok:
            ev = self.ev_model.estimate(
                sc.total, d.regime.regime, now, est.roundtrip_cost_frac, est.network_cost_frac, est.execution_risk_frac
            )
        else:
            ref_cost = fv.get("roundtrip_cost_frac", 0.1) or 0.1
            ev = self.ev_model.estimate(sc.total, d.regime.regime, now, ref_cost, 0.002, 0.01)
        d.ev = ev
        min_ev = cfg.strategy.min_expected_value_frac * ov.min_ev_multiplier
        if ev.status != "OK":
            d.gates.append(Gate("EXPECTED_VALUE", "FAIL", [f"INSUFFICIENT DATA ({ev.n} samples)"], ev.to_dict()))
        elif (ev.ev_conservative or -1) < min_ev:
            d.gates.append(
                Gate(
                    "EXPECTED_VALUE",
                    "FAIL",
                    [f"conservative EV {ev.ev_conservative:.2%} < required {min_ev:.2%} after costs"],
                    ev.to_dict(),
                )
            )
        else:
            d.gates.append(Gate("EXPECTED_VALUE", "PASS", [], ev.to_dict()))
        # POSITION SIZE
        d.gates.append(
            Gate(
                "POSITION_SIZE",
                "PASS" if sizing.size_sol > 0 else "FAIL",
                [sizing.rejected_reason] if sizing.rejected_reason else [],
                sizing.to_dict(),
            )
        )
        # EXECUTION SIMULATION
        if est is None:
            d.gates.append(Gate("EXECUTION_SIMULATION", "SKIPPED", ["no size"]))
        else:
            problems = self.sim.check_limits(
                est, cfg.filters.max_entry_price_impact, cfg.filters.max_expected_exit_slippage
            )
            d.gates.append(Gate("EXECUTION_SIMULATION", "FAIL" if problems else "PASS", problems, est.to_dict()))
        # RISK ENGINE
        rc = self.risk.check_entry(pf, d.mint, sizing.lamports, now, provider_busy)
        d.gates.append(Gate("RISK_ENGINE", "PASS" if rc.approved else "FAIL", rc.vetoes, rc.to_dict()))
        failed = [x for x in d.gates if x.status == "FAIL"]
        if not failed and est is not None:
            d.outcome = DecisionOutcome.APPROVED
            d.intent = OrderIntent(
                execution_id=new_execution_id(),
                mint=d.mint,
                side=Side.BUY,
                venue=adapter.venue,
                amount_in=sizing.lamports,
                min_out=est.minimum_output,
                expected_out=est.expected_output,
                expected_price=est.expected_price,
                spot_price=est.quoted_price,
                expected_slippage=est.expected_slippage,
                priority_micro_lamports=priority_micro_lamports,
                reason="entry",
                created_at=now,
                decision_id=d.decision_id,
                token_program=tm.record.token_program,
                creator=(tm.curve.creator if tm.curve else tm.record.creator),
                pool=tm.record.pool_address,
                meta={"mayhem": tm.record.is_mayhem},
            )
        elif all(x.name in ("EXPECTED_VALUE",) for x in failed):
            d.outcome = DecisionOutcome.NO_TRADE
        else:
            d.outcome = DecisionOutcome.REJECTED
        METRICS.inc(f"decision.{d.outcome.value}")
        return d

    def _finish_skipped(self, d: Decision, from_index: int) -> Decision:
        have = {g.name for g in d.gates}
        for name in GATES:
            if name not in have:
                d.gates.append(Gate(name, "SKIPPED"))
        d.outcome = DecisionOutcome.REJECTED
        METRICS.inc("decision.REJECTED")
        return d
