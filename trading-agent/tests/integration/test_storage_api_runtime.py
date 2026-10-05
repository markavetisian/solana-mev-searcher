from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest
from sqlalchemy import select

from helpers import CurveToken
from tradingagent.ai.analyst import GuardedAnalyst, NullAnalyst
from tradingagent.api.app import create_app
from tradingagent.common.config import AIConfig, AppConfig, Secrets
from tradingagent.common.types import Mode
from tradingagent.execution.providers import PaperExecutionProvider
from tradingagent.ingestion.sources import ListSource
from tradingagent.paper.runtime import TradingRuntime
from tradingagent.risk.killswitch import RESET_PHRASE
from tradingagent.storage import models as m
from tradingagent.storage.db import BatchWriter, Database
from tradingagent.storage.replay import load_db_events
from tradingagent.storage.sink import event_row

ADMIN, READER = "a" * 40, "r" * 40


def secrets(url: str) -> Secrets:
    return Secrets(database_url=url, api_admin_token=ADMIN, api_read_token=READER, _env_file=None)


async def test_market_events_roundtrip_and_dedup(sqlite_db):
    tok = CurveToken("db", t0=100.0).create().pump(101, 130)
    w = BatchWriter(sqlite_db)
    for ev in tok.events + tok.events[:5]:  # duplicates are ignored
        w.put(m.MarketEventRow.__table__, event_row(ev), ignore_conflicts=True)
    await w.flush()
    back = await load_db_events(sqlite_db)
    assert len(back) == len(tok.events)
    assert [e.event_id for e in back] == [e.event_id for e in tok.events]
    assert back[5].virtual_quote_reserves == tok.events[5].virtual_quote_reserves


async def test_runtime_state_and_upserts(sqlite_db):
    await sqlite_db.set_state("k", {"a": 1})
    await sqlite_db.set_state("k", {"a": 2})
    v, ts = await sqlite_db.get_state("k")
    assert v == {"a": 2} and ts is not None
    await sqlite_db.upsert_many(
        m.Token.__table__,
        [{"mint": "M", "market_state": "NEW", "first_seen_slot": 1, "last_seen_slot": 1, "first_seen_at": 1.0}],
        "mint",
    )
    await sqlite_db.upsert_many(
        m.Token.__table__,
        [{"mint": "M", "market_state": "DEAD", "first_seen_slot": 1, "last_seen_slot": 9, "first_seen_at": 1.0}],
        "mint",
    )
    async with sqlite_db.session() as s:
        t = (await s.execute(select(m.Token))).scalar_one()
    assert t.market_state == "DEAD" and t.last_seen_slot == 9


async def test_api_auth_kill_and_no_secret_leak(sqlite_db, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    cfg = AppConfig()
    app = create_app(cfg, secrets(url), sqlite_db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/system/status")).status_code == 401
        h = {"Authorization": f"Bearer {READER}"}
        st = await c.get("/system/status", headers=h)
        assert st.status_code == 200 and st.json()["worker"] == "NOT_RUNNING"
        assert (await c.post("/system/kill", json={"reason": "x"}, headers=h)).status_code == 401  # reader != admin
        ha = {"Authorization": f"Bearer {ADMIN}"}
        r = await c.post("/system/kill", json={"reason": "drill", "operator": "tester"}, headers=ha)
        assert r.status_code == 200 and r.json()["killed"]
        ks, _ = await sqlite_db.get_state("killswitch")
        assert ks["killed"] and ks["reason"] == "drill"  # latched even with no worker running
        assert (await c.post("/system/resume", json={}, headers=ha)).status_code == 409
        assert (await c.post("/system/resume", json={"confirm": RESET_PHRASE}, headers=ha)).status_code == 200
        bad = await c.post("/system/pause", json={"operator": "x; drop table"}, headers=ha)
        assert bad.status_code == 422
        for path in (
            "/opportunities",
            "/positions",
            "/portfolio",
            "/trades",
            "/risk",
            "/metrics",
            "/decisions",
            "/tokens",
            "/research/experiments",
            "/backtests",
        ):
            resp = await c.get(path, headers=h)
            assert resp.status_code == 200, path
            assert ADMIN not in resp.text and READER not in resp.text
        assert (await c.get("/tokens/not-a-mint!!", headers=h)).status_code == 400
    async with sqlite_db.session() as s:
        cmds = (await s.execute(select(m.OperatorCommand))).scalars().all()
    assert [x.command for x in cmds] == ["kill", "resume"]


async def test_post_endpoints_disabled_without_admin_token(sqlite_db, tmp_path):
    app = create_app(
        AppConfig(), Secrets(database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", _env_file=None), sqlite_db
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/system/kill", json={})).status_code == 403


async def test_paper_runtime_end_to_end(sqlite_db):
    """Full PAPER runtime on a replayed stream: ingest -> decisions -> persistence -> published state -> commands."""
    cfg = AppConfig().with_overrides(
        filters={
            "min_liquidity_sol": 1.0,
            "min_unique_traders_300s": 5,
            "min_volume_sol_300s": 1.0,
            "max_top10_concentration": 0.9,
            "min_token_age_s": 5,
            "max_data_staleness_s": 1e9,
        },
        execution={"assumed_latency_s": 0.0},
        backtest={"evaluation_interval_s": 0.05},
        strategy={"exit_check_interval_s": 0.05, "evaluation_cooldown_s": 0.0},
        regime={"degraded_staleness_s": 1e9},
        tracking={"snapshot_interval_s": 0.1},
    )
    now = time.time()
    tok = CurveToken("rt", t0=now - 200).create().pump(now - 199, now - 1, every=1.0, sol=0.2)
    rt = TradingRuntime(
        cfg,
        Mode.PAPER,
        ListSource(tok.events),
        PaperExecutionProvider(cfg.execution),
        sqlite_db,
        GuardedAnalyst(NullAnalyst(), AIConfig()),
    )
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(2.0)
    rt.apply_command("kill", {"reason": "test drill"}, "pytest")
    assert rt.core.kill.state.killed
    await asyncio.sleep(0.3)
    await rt.shutdown()
    task.cancel()
    st, _ = await sqlite_db.get_state("status")
    assert st["mode"] == "PAPER" and st["tracked_tokens"] >= 1
    ks, _ = await sqlite_db.get_state("killswitch")
    assert ks["killed"]
    async with sqlite_db.session() as s:
        n_events = len((await s.execute(select(m.MarketEventRow.id))).all())
        sigs = (await s.execute(select(m.Signal))).scalars().all()
        cfgs = (await s.execute(select(m.ConfigurationVersion))).scalars().all()
    assert n_events == len(tok.events)
    assert sigs and all(x.configuration_version == cfg.configuration_version for x in sigs)
    assert cfgs[0].version == cfg.configuration_version
    assert rt.apply_command("resume", {}, "pytest")["ok"] is False  # latched: needs the reset phrase
    assert rt.apply_command("resume", {"confirm": RESET_PHRASE}, "pytest")["ok"] is True


@pytest.mark.postgres
async def test_postgres_migrations_and_append_only_triggers():
    url = os.environ.get("TA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TA_TEST_POSTGRES_URL not set")
    import subprocess
    import sys

    env = dict(os.environ, DATABASE_URL=url)
    out = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    db = Database(url)
    try:
        await db.write_now(m.RiskEvent.__table__, {"t": 1.0, "kind": "TEST", "severity": "INFO", "detail": {}})
        from sqlalchemy import text

        with pytest.raises(Exception, match="append-only"):
            async with db.engine.begin() as conn:
                await conn.execute(text("UPDATE risk_events SET kind='HACKED'"))
        with pytest.raises(Exception, match="append-only"):
            async with db.engine.begin() as conn:
                await conn.execute(text("DELETE FROM risk_events"))
        await db.set_state("x", {"ok": True})  # mutable tables still work
    finally:
        await db.close()
