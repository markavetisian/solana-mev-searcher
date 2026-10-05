from __future__ import annotations

import pytest

from tradingagent.common.config import AppConfig, LiveLimits
from tradingagent.common.types import ExecStatus, ExitReason, Regime, Side, Venue
from tradingagent.execution.orders import ExecutionReport
from tradingagent.execution.simulator import ExecutionSimulator
from tradingagent.portfolio.portfolio import Portfolio
from tradingagent.pump.adapters import BondingCurveAdapter
from tradingagent.pump.curve import initial_state, quote_buy_exact_quote_in
from tradingagent.risk.engine import RiskEngine
from tradingagent.risk.killswitch import RESET_PHRASE, KillSwitch
from tradingagent.risk.sizing import PositionSizer, SizingInputs
from tradingagent.strategy.ev import EVModel, OutcomeSample, OutcomeStore
from tradingagent.strategy.exits import ExitPolicy

SOL = 10**9


def sample(t: float, score: float, gross: float, regime: str = "NORMAL", src: str = "paper") -> OutcomeSample:
    return OutcomeSample(
        t=t,
        score=score,
        regime=regime,
        gross_return=gross,
        net_return=gross - 0.03,
        hold_s=60,
        source=src,
        strategy_version="pump-momentum-0.1.0",
    )


# ---------------------------------------------------------------------------------------------------- EV ----
def test_ev_insufficient_data(cfg):
    m = EVModel(cfg.ev, OutcomeStore())
    est = m.estimate(85, Regime.NORMAL, now=1e9, roundtrip_cost_frac=0.03)
    assert est.status == "INSUFFICIENT_DATA" and est.ev is None and "INSUFFICIENT DATA" in est.notes[-1]


def test_ev_math_and_conservative_bound(cfg):
    store = OutcomeStore()
    for i in range(100):
        store.add(sample(1000 + i, 85, 0.20 if i % 2 == 0 else -0.10))
    est = EVModel(cfg.ev, store).estimate(85, Regime.NORMAL, now=5000, roundtrip_cost_frac=0.04)
    # wins: 0.20-0.04=0.16 (50%), losses: -0.10-0.04 -> 0.14
    assert est.status == "OK" and est.p_win == pytest.approx(0.5)
    assert est.ev == pytest.approx(0.5 * 0.16 - 0.5 * 0.14)
    assert est.ev_conservative < est.ev  # Wilson lower bound on P(win)


def test_ev_costs_can_turn_a_profitable_signal_unprofitable(cfg):
    store = OutcomeStore()
    for i in range(200):
        store.add(sample(i, 85, 0.03 if i % 2 == 0 else -0.01))  # +1% average gross
    m = EVModel(cfg.ev, store)
    assert m.estimate(85, Regime.NORMAL, 10_000, 0.0).ev > 0
    assert m.estimate(85, Regime.NORMAL, 10_000, 0.025).ev < 0  # after realistic costs: reject


def test_ev_never_uses_outcomes_from_the_future(cfg):
    store = OutcomeStore()
    for i in range(100):
        store.add(sample(10_000 + i, 85, 0.5))
    assert EVModel(cfg.ev, store).estimate(85, Regime.NORMAL, now=9_999, roundtrip_cost_frac=0.0).n == 0


def test_ev_regime_conditioning_falls_back_to_pooled(cfg):
    store = OutcomeStore()
    for i in range(60):
        store.add(sample(i, 85, 0.1, regime="NORMAL"))
    est = EVModel(cfg.ev, store).estimate(85, Regime.RISK_OFF, 1000, 0.0)
    assert est.status == "OK" and not est.conditioned_on_regime and est.notes


# ------------------------------------------------------------------------------------------------ sizing ----
def inputs(**kw) -> SizingInputs:
    base = dict(
        equity_sol=10.0,
        cash_sol=10.0,
        open_exposure_sol=0.0,
        daily_pnl_sol=0.0,
        hard_stop_frac=0.15,
        entry_slippage_frac=0.02,
        exit_slippage_frac=0.02,
        network_cost_frac=0.002,
        liquidity_sol=100.0,
        volatility_60s=0.03,
        execution_success_rate=1.0,
        score=95.0,
        min_score=70.0,
        regime_multiplier=1.0,
    )
    base.update(kw)
    return SizingInputs(**base)


def test_high_score_never_means_big_size(cfg):
    r = PositionSizer(cfg.risk).size(inputs(score=99.9))
    assert r.size_sol <= cfg.risk.max_position_pct * 10.0 + 1e-9
    assert r.size_sol <= 10.0 * cfg.risk.max_risk_per_trade / (0.15 + 0.02 + 0.02 + 0.002) + 1e-9


def test_sizing_caps_and_binding_constraints(cfg):
    assert PositionSizer(cfg.risk).size(inputs(liquidity_sol=5.0)).binding_constraint == "liquidity_share"
    assert PositionSizer(cfg.risk).size(inputs(open_exposure_sol=2.45)).size_sol < 0.06
    r = PositionSizer(cfg.risk).size(inputs(liquidity_sol=0.5))
    assert r.size_sol == 0.0 and "below minimum" in r.rejected_reason


def test_sizing_scales_down_with_volatility_drawdown_regime(cfg):
    base = PositionSizer(cfg.risk).size(inputs()).size_sol
    assert PositionSizer(cfg.risk).size(inputs(volatility_60s=0.3)).size_sol < base
    assert PositionSizer(cfg.risk).size(inputs(daily_pnl_sol=-0.4)).size_sol < base
    assert PositionSizer(cfg.risk).size(inputs(regime_multiplier=0.25)).size_sol < base
    assert PositionSizer(cfg.risk).size(inputs(daily_pnl_sol=-0.5)).size_sol == 0.0


def test_live_limits_are_independent_ceiling(cfg):
    r = PositionSizer(cfg.risk, LiveLimits(max_position_sol=0.05)).size(inputs(equity_sol=1000, cash_sol=1000))
    assert r.size_sol <= 0.05


# ----------------------------------------------------------------------------------------------- portfolio ----
def report(eid: str, side: Side, actual_in: int, actual_out: int, spot: float, price: float) -> ExecutionReport:
    return ExecutionReport(
        execution_id=eid,
        status=ExecStatus.FILLED_PAPER,
        mint="M",
        side=side,
        venue=Venue.BONDING_CURVE,
        requested_amount=actual_in,
        actual_in=actual_in,
        actual_out=actual_out,
        network_fees=10_000,
        platform_fees=1000,
        spot_at_fill=spot,
        actual_price=price,
    )


def open_pos(pf: Portfolio, cfg: AppConfig):
    return pf.open_position(
        report("e1", Side.BUY, SOL, 1_000_000, 900.0, 1000.0),
        100.0,
        symbol="M",
        decision_id="d",
        expected_entry_slippage=0.1,
        hard_stop_frac=0.15,
        take_profits=[(0.2, 0.5), (0.4, 1.0)],
        trailing_stop_frac=0.1,
        trailing_activation_gain=0.1,
        max_hold_s=900,
        score=80,
        regime="NORMAL",
        thesis={},
        versions={"strategy_version": "s"},
    )


def test_portfolio_partial_then_full_exit_accounting(cfg):
    pf = Portfolio(10 * SOL, 0.0)
    pos = open_pos(pf, cfg)
    assert pf.cash == 9 * SOL - 10_000
    ct = pf.apply_exit(pos, report("e2", Side.SELL, 500_000, 700_000_000, 1300.0, 1400.0), 200.0, "TAKE_PROFIT")
    assert ct is None and pos.tokens == 500_000 and pos.cost_lamports == SOL // 2
    ct = pf.apply_exit(pos, report("e3", Side.SELL, 500_000, 400_000_000, 900.0, 800.0), 300.0, "HARD_STOP")
    assert ct is not None and not pf.positions
    assert ct.pnl_lamports == 1_100_000_000 - SOL - 30_000
    assert ct.gross_return == pytest.approx(((1300 + 900) / 2) / 900 - 1)
    assert pf.equity == pf.cash


# ------------------------------------------------------------------------------------------------- exits ----
def test_exit_rules(cfg):
    from helpers import CurveToken
    from tradingagent.common.clock import ManualClock
    from tradingagent.market.engine import MarketDataEngine
    from tradingagent.pump.fees import FeeSchedule

    sched = FeeSchedule.from_rows(cfg.pump.fee_tiers, cfg.pump.flat_fees)
    tok = CurveToken("ex", t0=0).create().pump(1, 100)
    clock = ManualClock(0)
    eng = MarketDataEngine(cfg, clock, sched)
    for ev in tok.events:
        clock.advance_to(ev.t)
        eng.on_event(ev)
    tm = eng.market(tok.mint)
    pf = Portfolio(10 * SOL, 0)
    pos = open_pos(pf, cfg)
    pos.opened_at = 90.0
    pol = ExitPolicy(cfg.strategy)
    pos.last_value_lamports = int(0.80 * SOL)
    assert pol.evaluate(pos, tm, None, 100, True).reason is ExitReason.HARD_STOP
    pos.last_value_lamports = int(1.25 * SOL)
    sig = pol.evaluate(pos, tm, None, 100, True)
    assert sig.reason is ExitReason.TAKE_PROFIT and sig.fraction == pytest.approx(0.5)  # the position's own ladder
    pos.peak_return = 0.5
    pos.last_value_lamports = int(1.30 * SOL)
    assert pol.evaluate(pos, tm, None, 100, True).reason is ExitReason.TRAILING_STOP
    pos.peak_return, pos.last_value_lamports, pos.tp_hits = 0.02, int(1.0 * SOL), 3
    assert pol.evaluate(pos, tm, None, 90 + cfg.strategy.time_stop_s + 1, True).reason is ExitReason.TIME_STOP
    assert pol.evaluate(pos, tm, None, 100, tradeable=False) is None  # e.g. graduating: hold, no venue


# --------------------------------------------------------------------------------------------- risk/kill ----
def test_risk_engine_vetoes(cfg):
    kill = KillSwitch(cfg.killswitch, lambda: 1000.0)
    re = RiskEngine(cfg.risk, kill, reentry_cooldown_s=600)
    pf = Portfolio(10 * SOL, 0)
    assert re.check_entry(pf, "M", int(0.3 * SOL), 1000).approved
    kill.pause("maintenance")
    assert any("paused" in v for v in re.check_entry(pf, "M", int(0.3 * SOL), 1000).vetoes)
    kill.resume()
    re.note_exit("M", 900)
    assert any("cooldown" in v for v in re.check_entry(pf, "M", int(0.3 * SOL), 1000).vetoes)
    assert any("max_position_pct" in v for v in re.check_entry(pf, "X", 2 * SOL, 1000).vetoes)
    pf.cash -= int(0.6 * SOL)  # simulate a realized daily loss
    assert any("daily loss" in v for v in re.check_entry(pf, "Y", int(0.1 * SOL), 1000).vetoes)


def test_kill_switch_latches_and_needs_explicit_reset(cfg):
    t = [0.0]
    states = []
    k = KillSwitch(cfg.killswitch, lambda: t[0], on_change=lambda s, a: states.append(a))
    for i in range(cfg.killswitch.max_execution_failures):
        t[0] += 1
        k.record_execution_failure(f"f{i}")
    assert k.state.killed and not k.entries_allowed and not k.orders_allowed
    assert not k.reset("op", "please")
    assert k.state.killed
    assert k.reset("op", RESET_PHRASE) and not k.state.killed
    assert states == ["KILL", "RESET"]


def test_kill_switch_auto_triggers(cfg):
    t = [0.0]
    k = KillSwitch(cfg.killswitch, lambda: t[0])
    k.observe_staleness(100.0)
    t[0] = cfg.killswitch.stale_data_kill_after_s + 1
    k.observe_staleness(100.0)
    assert k.state.killed and "stale" in k.state.reason
    k2 = KillSwitch(cfg.killswitch, lambda: 0.0)
    for _ in range(cfg.killswitch.db_error_kill_after):
        k2.record_db_error("down")
    assert k2.state.killed
    k3 = KillSwitch(cfg.killswitch, lambda: 0.0)
    k3.uncertain_transaction("ex_1")
    assert k3.state.killed
    k4 = KillSwitch(cfg.killswitch, lambda: 0.0)
    k4.restore({"killed": True, "reason": "persisted"})
    assert k4.state.killed


# --------------------------------------------------------------------------------------------- simulator ----
def test_execution_simulator_costs(cfg, sched):
    st = initial_state(creator="11111111111111111111111111111112")
    st = quote_buy_exact_quote_in(st, 20 * SOL, sched).state_after
    sim = ExecutionSimulator(cfg.execution)
    est = sim.simulate_entry(BondingCurveAdapter(st, sched), SOL, 100_000, 0.05)
    assert est.ok and est.expected_output > 0 and est.minimum_output < est.expected_output
    assert est.platform_fees > 0 and est.priority_fee == cfg.execution.compute_unit_limit * 100_000 // 10**6
    assert 0 < est.price_impact < est.expected_slippage
    assert est.roundtrip_cost_frac > 2 * 0.0123 * 0.9  # both legs pay the tier fee
    assert est.total_cost_frac > est.roundtrip_cost_frac
    big = sim.simulate_entry(BondingCurveAdapter(st, sched), 30 * SOL, 100_000, 0.05)
    assert sim.check_limits(big, 0.04, 0.07)  # rejected: impact / exit slippage too large
