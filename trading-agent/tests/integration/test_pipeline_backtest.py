from __future__ import annotations

import pytest

from helpers import CurveToken, key
from tradingagent.ai.analyst import AIResult, parse_assessment
from tradingagent.backtest.engine import Backtester, SyntheticDataRefused
from tradingagent.backtest.metrics import edge_verdict, performance
from tradingagent.backtest.walkforward import make_folds, run_walkforward
from tradingagent.common.clock import ManualClock
from tradingagent.common.config import AppConfig
from tradingagent.common.types import DecisionOutcome, Mode
from tradingagent.ingestion.synthetic import SyntheticMarket
from tradingagent.strategy.core import build_core
from tradingagent.strategy.ev import OutcomeSample, OutcomeStore


def permissive_cfg() -> AppConfig:
    """Loosened filters so a single hand-built token can pass every deterministic gate."""
    return AppConfig().with_overrides(
        filters={
            "min_liquidity_sol": 1.0,
            "min_unique_traders_300s": 5,
            "min_volume_sol_300s": 1.0,
            "max_top10_concentration": 0.9,
            "max_entry_price_impact": 0.2,
            "max_expected_exit_slippage": 0.3,
            "min_token_age_s": 5,
            "max_volatility_60s": 5.0,
        },
        strategy={"min_score": 0.0, "min_expected_value_frac": -1.0},
        execution={"max_total_cost_frac": 0.5, "paper_failure_rate": 0.0},
        ev={"min_samples": 5, "condition_on_regime": False},
        regime={"low_activity_trades_per_min": 0.0, "degraded_staleness_s": 1e9},
    )


def seeded_outcomes(n: int = 120) -> OutcomeStore:
    s = OutcomeStore()
    for i in range(n):
        s.add(
            OutcomeSample(
                t=0.0 + i,
                score=float((i % 10) * 10 + 5),
                regime="NORMAL",
                gross_return=0.5 if i % 3 else -0.1,
                net_return=0.4,
                hold_s=60,
                source="paper",
                strategy_version="pump-momentum-0.1.0",
            )
        )
    return s


def run_core_to(cfg: AppConfig, tok: CurveToken, outcomes: OutcomeStore | None = None):
    clock = ManualClock(tok.t0)
    core = build_core(cfg, clock, Mode.PAPER, outcomes=outcomes)
    for ev in tok.events:
        clock.advance_to(ev.t)
        core.ingest(ev)
    return core, clock


def test_every_gate_is_recorded_in_order_and_ai_can_only_veto():
    cfg = permissive_cfg()
    tok = CurveToken("gate", t0=1000.0).create().pump(1001.0, 1200.0, every=1.0, sol=0.3)
    core, clock = run_core_to(cfg, tok, seeded_outcomes())
    now = clock.now()
    core.refresh_regime(now)
    d = core.stage1(tok.mint, now, 10_000)
    assert d.stage1_passed, d.rejection_reasons
    ok = core.stage2(d, AIResult(status="TIMEOUT"), now, 10_000)
    names = [g.name for g in ok.gates]
    assert names == [
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
    ]
    assert ok.outcome is DecisionOutcome.APPROVED, ok.rejection_reasons  # AI unavailable: deterministic path continues
    assert ok.intent is not None and ok.intent.min_out < ok.intent.expected_out
    for k in ("strategy_version", "feature_version", "configuration_version", "model_version", "ai_model"):
        assert ok.versions[k]
    d2 = core.stage1(tok.mint, now, 10_000)
    veto = AIResult(
        status="OK",
        assessment=parse_assessment(
            '{"assessment":"negative","confidence":0.95,"risk_flags":["wash_trading"],"observations":[],"thesis":"","contradictions":[]}'
        ),
    )
    vetoed = core.stage2(d2, veto, now, 10_000)
    assert vetoed.outcome is DecisionOutcome.REJECTED and vetoed.gate("AI_ANALYSIS").status == "FAIL"
    # a POSITIVE AI opinion cannot rescue a deterministic rejection
    core.kill.pause("test")
    d3 = core.stage1(tok.mint, now, 10_000)
    positive = AIResult(
        status="OK",
        assessment=parse_assessment(
            '{"assessment":"positive","confidence":1.0,"risk_flags":[],"observations":[],"thesis":"x","contradictions":[]}'
        ),
    )
    assert core.stage2(d3, positive, now, 10_000).outcome is DecisionOutcome.REJECTED


def test_ai_required_for_entry_blocks_when_unavailable():
    cfg = permissive_cfg().with_overrides(ai={"enabled": True, "required_for_entry": True})
    tok = CurveToken("req", t0=0.0).create().pump(1.0, 200.0, every=1.0, sol=0.3)
    core, clock = run_core_to(cfg, tok, seeded_outcomes())
    d = core.stage1(tok.mint, clock.now(), 1000)
    out = core.stage2(d, AIResult(status="MALFORMED"), clock.now(), 1000)
    assert out.outcome is DecisionOutcome.REJECTED and out.gate("AI_ANALYSIS").status == "FAIL"


def test_insufficient_data_means_no_trade():
    cfg = permissive_cfg().with_overrides(ev={"min_samples": 50})
    tok = CurveToken("nodata", t0=0.0).create().pump(1.0, 200.0, every=1.0, sol=0.3)
    core, clock = run_core_to(cfg, tok, OutcomeStore())
    d = core.stage2(core.stage1(tok.mint, clock.now(), 1000), AIResult(status="DISABLED"), clock.now(), 1000)
    assert d.outcome is DecisionOutcome.NO_TRADE and d.intent is None
    assert "INSUFFICIENT DATA" in d.gate("EXPECTED_VALUE").reasons[0]


def test_backtest_end_to_end_position_lifecycle_is_deterministic():
    cfg = permissive_cfg().with_overrides(
        strategy={"min_score": 0.0, "min_expected_value_frac": -1.0, "reentry_cooldown_s": 1e9}
    )
    toks = [CurveToken(f"bt{i}", t0=1000.0 + 50 * i).create() for i in range(3)]
    for i, t in enumerate(toks):
        t.pump(t.t0 + 1, t.t0 + 200, every=1.0, sol=0.3).dump(t.t0 + 200, t.t0 + 260)
    events = sorted([e for t in toks for e in t.events], key=lambda e: e.order_key)
    a = Backtester(cfg, outcomes=seeded_outcomes(), seed=1, calibration=False).run(events)
    b = Backtester(cfg, outcomes=seeded_outcomes(), seed=1, calibration=False).run(events)
    assert [t.pnl_lamports for t in a.trades] == [t.pnl_lamports for t in b.trades]
    assert len(a.trades) >= 1 and a.executions.get("total", 0) >= 2
    for t in a.trades:
        assert t.closed_at >= t.opened_at + cfg.execution.assumed_latency_s - 1e-9
        assert t.versions["configuration_version"] == cfg.configuration_version
    perf = performance(a.trade_dicts(), a.equity_curve, a.executions)
    assert {
        "win_rate",
        "avg_winner",
        "avg_loser",
        "expectancy_per_trade",
        "profit_factor",
        "max_drawdown",
        "median_hold_s",
        "cvar_5",
        "fees_sol",
        "failed_execution_rate",
    } <= set(perf)


def test_backtest_refuses_synthetic_data_by_default():
    ev = SyntheticMarket(seed=1, duration_s=60).generate()
    with pytest.raises(SyntheticDataRefused):
        Backtester(AppConfig()).run(ev)


def test_fills_happen_after_latency_against_later_state():
    """A large competing buy between decision and fill must worsen our fill (no look-ahead, no free lunch)."""
    cfg = permissive_cfg().with_overrides(
        execution={
            "assumed_latency_s": 2.0,
            "paper_failure_rate": 0.0,
            "max_slippage_bps": 5000,
            "max_total_cost_frac": 0.9,
        },
        strategy={"min_score": 0.0, "min_expected_value_frac": -1.0},
    )
    tok = CurveToken("lat", t0=0.0).create().pump(1.0, 150.0, every=1.0, sol=0.3)
    base = list(tok.events)
    front = CurveToken("lat", t0=0.0)
    front.events, front.state, front.holdings, front._n = list(base), tok.state, dict(tok.holdings), tok._n
    front.buy(151.0, key("frontrunner"), 15.0)
    quiet = Backtester(cfg, outcomes=seeded_outcomes(), calibration=False).run(base)
    noisy = Backtester(cfg, outcomes=seeded_outcomes(), calibration=False).run(front.events)
    if quiet.trades and noisy.trades:
        assert noisy.trades[0].actual_entry_slippage >= quiet.trades[0].actual_entry_slippage


def test_walkforward_structure_and_verdict():
    cfg = AppConfig().with_overrides(backtest={"min_test_trades": 5, "bootstrap_samples": 200, "allow_synthetic": True})
    ev = SyntheticMarket(seed=2, planted_edge=0.0, duration_s=1800, launches_per_min=3).generate()
    res = run_walkforward(cfg, ev, train_days=900 / 86400, test_days=450 / 86400, step_days=450 / 86400)
    assert res["folds"] and "verdict" in res and res["configs_tried_per_fold"] == 3
    assert res["synthetic_data"] is True
    for f in res["folds"]:
        assert f["train"][1] <= f["test"][0]  # strict separation
    assert res["verdict"]["verdict"] in (
        "INSUFFICIENT DATA",
        "NO EDGE DEMONSTRATED",
        "EDGE SUPPORTED (out-of-sample, after costs)",
    )


def test_folds_never_overlap():
    folds = make_folds(0, 10 * 86400, 3 * 86400, 1 * 86400, 1 * 86400)
    assert len(folds) == 7
    for f in folds:
        assert f.train[1] == f.test[0]


def test_edge_verdict_requires_positive_ci_and_folds():
    trades = [
        {
            "net_return": 0.01 * ((-1) ** i),
            "pnl_lamports": 10**6 * ((-1) ** i),
            "hold_s": 10,
            "network_fees": 0,
            "platform_fees": 0,
            "expected_entry_slippage": 0,
            "actual_entry_slippage": 0,
            "exit_reason": "X",
            "regime": "NORMAL",
        }
        for i in range(100)
    ]
    v = edge_verdict(performance(trades), 30, [0.001, -0.001])
    assert v["verdict"] == "NO EDGE DEMONSTRATED"
    assert edge_verdict(performance(trades[:10]), 30, [])["verdict"] == "INSUFFICIENT DATA"
