from __future__ import annotations

import math

import pytest

from helpers import CurveToken, key
from tradingagent.common.clock import ClockWentBackwards, ManualClock
from tradingagent.common.config import AppConfig, ScoringConfig
from tradingagent.common.types import LifecycleState
from tradingagent.discovery.registry import InvalidTransition, TokenRegistry
from tradingagent.features.engine import FeatureEngine, FeatureVector
from tradingagent.market.engine import MarketDataEngine
from tradingagent.market.series import LookAheadError, Trade, TradeSeries
from tradingagent.scoring.model import Scorer, map_linear
from tradingagent.wallets.entities import CreatorBook, EntityGraph, WalletBook, assess_creator, wilson_lower


def build(cfg, sched, tok: CurveToken):
    clock = ManualClock(tok.t0)
    eng = MarketDataEngine(cfg, clock, sched)
    for ev in tok.events:
        clock.advance_to(ev.t)
        eng.on_event(ev)
    fe = FeatureEngine(sched, WalletBook(), CreatorBook(), EntityGraph())
    return clock, eng, fe


def test_series_window_semantics_and_lookahead_guard():
    s = TradeSeries()
    for i in range(10):
        s.append(
            Trade(
                t=float(i),
                slot=i,
                side=1 if i % 2 else -1,
                sol=10**9,
                tokens=1,
                user=f"u{i}",
                price=1.0 + i,
                liquidity=1,
                venue="BONDING_CURVE",
            )
        )
    assert [r.t for r in s.window(5.0, 2.0)] == [4.0, 5.0]  # (3, 5]
    assert s.volume_between(3.0, 5.0) == (10**9, 10**9, 2)
    assert s.last_before(4.5).t == 4.0
    with pytest.raises(LookAheadError):
        s.window(6.0, 1.0, now=5.0)


def test_prefix_sums_equal_naive_sums():
    s = TradeSeries()
    import random

    rng = random.Random(1)
    t = 0.0
    for _ in range(2000):
        t += rng.random()
        s.append(
            Trade(
                t=t,
                slot=0,
                side=rng.choice((1, -1)),
                sol=rng.randint(1, 10**9),
                tokens=1,
                user="u",
                price=1.0,
                liquidity=1,
                venue="",
            )
        )
    for a, b in [(10.0, 100.0), (0.0, 5.0), (500.0, 900.0), (t - 30, t)]:
        rows = s.between(a, b)
        naive = (sum(r.sol for r in rows if r.side > 0), sum(r.sol for r in rows if r.side < 0), len(rows))
        assert s.volume_between(a, b) == naive


def test_manual_clock_refuses_to_go_backwards():
    c = ManualClock(10.0)
    c.advance_to(11.0)
    with pytest.raises(ClockWentBackwards):
        c.advance_to(10.5)


def test_features_on_a_pumping_token(cfg, sched):
    tok = CurveToken("pumpy", t0=1000.0).create().pump(1001.0, 1200.0, every=1.0, sol=0.5)
    clock, eng, fe = build(cfg, sched, tok)
    fv = fe.compute(eng.market(tok.mint), clock.now(), 0.1)
    assert fv.get("ret_60s") > 0 and fv.get("ret_300s") > 0
    assert fv.get("imbalance_60s") > 0
    assert fv.get("n_trades_60s") == pytest.approx(60, abs=2)
    assert fv.get("holders") > 20
    assert fv.get("partial_history") == 0.0
    assert 0 < fv.get("entry_impact_frac") < fv.get("roundtrip_cost_frac")
    assert fv.get("creator_holding") == 0.0  # creator never bought in this stream


def test_features_never_see_the_future(cfg, sched):
    """Same token, two engines: one fed everything, one fed only up to T. Features as of T must be identical."""
    tok = CurveToken("lk", t0=0.0).create().pump(1.0, 300.0, every=0.7, sol=0.12).dump(300.0, 400.0)
    T = 200.0
    _, full, fe1 = build(cfg, sched, tok)
    clock = ManualClock(0.0)
    part = MarketDataEngine(cfg, clock, sched)
    for ev in tok.events:
        if ev.t > T:
            break
        clock.advance_to(ev.t)
        part.on_event(ev)
    fe2 = FeatureEngine(sched, WalletBook(), CreatorBook(), EntityGraph())
    a = fe2.compute(part.market(tok.mint), T, 0.0).values
    # The fully-fed engine's state already contains events after T: asking it for features "as of T" must fail
    # loudly instead of silently mixing current reserves/holders into historical features.
    with pytest.raises(LookAheadError):
        fe1.compute(full.market(tok.mint), T, 0.0)
    # and the engine that only saw data up to T produces sane, finite features
    assert a["ret_60s"] is not None and a["n_trades_60s"] > 0


def test_partial_history_tokens_have_unknown_holder_features(cfg, sched):
    tok = CurveToken("late", t0=0.0).create().pump(1.0, 100.0)
    tok.events = tok.events[30:]  # we never saw the CreateEvent nor the first trades
    clock, eng, fe = build(cfg, sched, tok)
    fv = fe.compute(eng.market(tok.mint), clock.now(), 0.0)
    assert fv.get("partial_history") == 1.0
    assert fv.get("top10_share") is None and fv.get("creator_holding") is None


def test_reserve_mismatch_flags_missing_events(cfg, sched):
    tok = CurveToken("gap", t0=0.0).create().pump(1.0, 60.0)
    del tok.events[20]
    _, eng, _ = build(cfg, sched, tok)
    tm = eng.market(tok.mint)
    assert tm.reserve_mismatches == 1 and tm.last_gap_at is not None


def test_lifecycle_state_machine():
    reg = TokenRegistry()
    rec, _ = reg.ensure("m", 0.0, 1, from_create=True)
    reg.transition(rec, LifecycleState.BONDING_CURVE, 0, "create")
    reg.transition(rec, LifecycleState.GRADUATING, 1, "complete")
    with pytest.raises(InvalidTransition):
        reg.transition(rec, LifecycleState.BONDING_CURVE, 2, "nope")
    reg.transition(rec, LifecycleState.PUMPSWAP, 3, "pool")
    reg.transition(rec, LifecycleState.ACTIVE, 4, "history")
    assert [h[1] for h in rec.history] == ["BONDING_CURVE", "GRADUATING", "PUMPSWAP", "ACTIVE"]


def test_graduation_flow_switches_venue(cfg, sched):
    tok = CurveToken("grad", t0=0.0).create()
    tok.buy(1.0, key("whale"), 120.0)  # exhausts the curve
    _, eng, _ = build(cfg, sched, tok)
    tm = eng.market(tok.mint)
    assert tm.record.state is LifecycleState.GRADUATING
    assert tm.adapter(sched) is None  # no venue while graduating


def test_map_linear_and_inversion():
    assert map_linear(5, 0, 10) == 50
    assert map_linear(-1, 0, 10) == 0 and map_linear(11, 0, 10) == 100
    assert map_linear(0.0, 0.08, 0.0) == 100  # lower is better
    assert map_linear(0.08, 0.08, 0.0) == 0


def test_scoring_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        ScoringConfig(
            weights={
                "momentum": 0.5,
                "volume": 0.6,
                "flow": 0,
                "holders": 0,
                "liquidity": 0,
                "wallets": 0,
                "creator": 0,
                "volatility": 0,
                "structure": 0,
                "exit_quality": 0,
            }
        )


def test_score_is_explainable_and_missing_is_not_neutral():
    cfg = AppConfig()
    sc = Scorer(cfg.scoring).score(FeatureVector("m", 0.0, {}))
    assert sc.total == 0.0
    assert all(c.missing for c in sc.components.values())
    text = sc.explain()
    assert "TOKEN SCORE: 0" in text and "Momentum" in text
    full = {m.feature: (m.hi if m.lo < m.hi else m.hi) for maps in cfg.scoring.components.values() for m in maps}
    sc2 = Scorer(cfg.scoring).score(FeatureVector("m", 0.0, full))
    assert sc2.total == pytest.approx(100.0)


def test_wilson_and_creator_assessment_confidence():
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(9, 10) < 0.9 and wilson_lower(90, 100) > wilson_lower(9, 10)
    weak = assess_creator(
        {"creator_prev_launches": 1, "creator_prev_rug_rate": 1.0, "creator_launches_24h": 1}, 0, 0.0, 6
    )
    strong = assess_creator(
        {"creator_prev_launches": 20, "creator_prev_rug_rate": 0.9, "creator_launches_24h": 12}, 9, 0.2, 6
    )
    assert strong.confidence > weak.confidence and strong.suspicion > 0.6
    assert weak.confidence < 0.5  # one bad launch is not a verdict
    assert not math.isnan(strong.suspicion)


def test_wallet_book_realized_pnl_and_smart_flag():
    from tradingagent.common.events import MarketEvent
    from tradingagent.common.types import EventKind, Side, Venue

    wb = WalletBook(smart_min_closed=3, smart_min_wilson=0.3)
    w = "W"
    t = 0.0
    for i in range(6):
        for side, sol in ((Side.BUY, 10**9), (Side.SELL, 2 * 10**9)):
            t += 1
            wb.on_trade(
                MarketEvent(
                    kind=EventKind.TRADE,
                    signature=f"s{t}",
                    slot=1,
                    seq=0,
                    observed_at=t,
                    chain_time=t,
                    mint=f"m{i}",
                    venue=Venue.BONDING_CURVE,
                    side=side,
                    user=w,
                    sol_amount=sol,
                    token_amount=1000,
                ),
                token_birth=0.0,
            )
    st = wb.get(w)
    assert st.closed == 6 and st.wins == 6 and st.realized_pnl == 6 * 10**9
    assert w in wb.smart
