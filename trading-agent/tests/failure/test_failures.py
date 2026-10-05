"""Failure handling: RPC failure, websocket drop, stale data, duplicate execution, failed transactions,
database outage, AI timeout / malformed output."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
import websockets

from helpers import CurveToken, key
from tradingagent.common.clock import ManualClock
from tradingagent.common.config import AppConfig
from tradingagent.common.types import ExecStatus, Mode, Side, Venue
from tradingagent.execution.orders import ExecutionReport, OrderIntent
from tradingagent.execution.providers import PaperExecutionProvider
from tradingagent.ingestion.parser import PUMP, LogParser
from tradingagent.ingestion.sources import WebSocketLogSource
from tradingagent.pump.borsh import pump_codec
from tradingagent.solana.rpc import RpcPool, RpcUnavailable
from tradingagent.solana.tx import DuplicateExecution
from tradingagent.storage.db import BatchWriter, Database, DatabaseUnavailable
from tradingagent.strategy.core import build_core


async def test_rpc_failover_and_circuit_breaker():
    calls = {"a": 0, "b": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        calls[host] += 1
        if host == "a":
            return httpx.Response(503)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 42})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool = RpcPool(["http://a", "http://b"], retries=1, circuit_open_after=2, circuit_cooldown_s=60, client=client)
    assert await pool.get_slot() == 42  # a fails, b answers
    assert await pool.get_slot() == 42
    assert calls["a"] == 1  # the failing endpoint is deprioritised immediately
    assert pool.health()[0]["consecutive_errors"] == 1
    await client.aclose()
    # a single persistently failing endpoint gets its circuit opened and is skipped entirely
    client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solo = RpcPool(["http://a"], retries=2, circuit_open_after=2, circuit_cooldown_s=60, client=client2)
    with pytest.raises(RpcUnavailable):
        await solo.get_slot()
    assert solo.endpoints[0].is_open
    before = calls["a"]
    with pytest.raises(RpcUnavailable):
        await solo.get_slot()
    assert calls["a"] == before
    await client2.aclose()


async def test_rpc_all_down_raises_unavailable():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    pool = RpcPool(["http://a"], retries=1, circuit_open_after=1, client=client)
    with pytest.raises(RpcUnavailable):
        await pool.get_slot()
    assert not pool.healthy
    await client.aclose()


async def test_rpc_application_error_is_not_retried_elsewhere():
    from tradingagent.solana.rpc import RpcError

    hits = []

    def handler(req):
        hits.append(req.url.host)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "sim failed"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool = RpcPool(["http://a", "http://b"], client=client)
    with pytest.raises(RpcError):
        await pool.call("simulateTransaction", [])
    assert hits == ["a"]
    await client.aclose()


async def test_websocket_ingestion_reconnect_produces_data_gap():
    tok = CurveToken("ws", t0=0).create().pump(1, 5)
    vals = {f["name"]: 0 for f in pump_codec().types["TradeEvent"]["type"]["fields"]}
    vals.update({k: key(k) for k in ("mint", "user", "fee_recipient", "creator", "quote_mint")})
    vals.update(
        {
            "mint": tok.mint,
            "is_buy": True,
            "ix_name": "buy",
            "shareholders": [],
            "track_volume": False,
            "mayhem_mode": False,
            "token_amount": 5,
            "sol_amount": 7,
            "virtual_token_reserves": 10,
            "virtual_sol_reserves": 10,
            "quote_mint": "11111111111111111111111111111111",
        }
    )
    data = base64.b64encode(pump_codec().encode_event("TradeEvent", vals)).decode()
    connections = {"n": 0}

    async def server(ws):
        connections["n"] += 1
        await ws.recv()
        await ws.recv()
        for i in range(2):
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "logsNotification",
                        "params": {
                            "result": {
                                "context": {"slot": 100 + i + connections["n"] * 10},
                                "value": {
                                    "signature": f"s{connections['n']}-{i}",
                                    "err": None,
                                    "logs": [
                                        f"Program {PUMP} invoke [1]",
                                        f"Program data: {data}",
                                        f"Program {PUMP} success",
                                    ],
                                },
                            }
                        },
                    }
                )
            )
        if connections["n"] == 1:
            await ws.close()  # drop the connection -> client must reconnect and flag a gap
        else:
            await asyncio.sleep(5)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        src = WebSocketLogSource([f"ws://127.0.0.1:{port}"], LogParser(), stale_after_s=3, max_backoff_s=0.2)
        got = []

        async def consume() -> None:
            async for ev in src.events():
                got.append(ev)
                if len(got) >= 5:
                    src.stop()
                    return

        await asyncio.wait_for(consume(), timeout=10)
    kinds = [e.kind.value for e in got]
    assert kinds.count("TRADE") >= 4 and "DATA_GAP" in kinds
    assert connections["n"] >= 2


async def test_database_outage_fails_closed(tmp_path):
    cfg = AppConfig()
    kill_reasons: list[str] = []
    db = Database("postgresql+asyncpg://nobody:nopass@127.0.0.1:1/none")  # nothing listens on port 1
    w = BatchWriter(db, on_error=kill_reasons.append)
    from tradingagent.storage import models as m

    w.put(m.RiskEvent.__table__, {"t": 1.0, "kind": "x", "severity": "INFO", "detail": {}})
    await w.flush()
    assert kill_reasons and w.backlog == 1  # row retained for retry, error reported to the kill switch
    with pytest.raises(DatabaseUnavailable):
        await db.write_now(m.Order.__table__, {"execution_id": "x"})
    assert not await db.ping()
    core = build_core(cfg, ManualClock(1.0), Mode.PAPER)
    for r in range(cfg.killswitch.db_error_kill_after):
        core.kill.record_db_error(kill_reasons[0])
    assert core.kill.state.killed and not core.kill.entries_allowed
    await db.close()


def intent(eid: str, mint: str = "M") -> OrderIntent:
    return OrderIntent(
        execution_id=eid,
        mint=mint,
        side=Side.BUY,
        venue=Venue.BONDING_CURVE,
        amount_in=10**8,
        min_out=1,
        expected_out=2,
        expected_price=1.0,
        spot_price=1.0,
        expected_slippage=0.0,
        priority_micro_lamports=0,
        reason="t",
        created_at=0.0,
    )


async def test_duplicate_execution_is_refused():
    p = PaperExecutionProvider(AppConfig().execution, simulate_latency=False)
    await p.execute(intent("e1"), lambda m: None)
    with pytest.raises(DuplicateExecution):
        await p.execute(intent("e1"), lambda m: None)


async def test_concurrent_orders_for_same_token_are_refused():
    p = PaperExecutionProvider(AppConfig().execution.model_copy(update={"assumed_latency_s": 0.2}))
    t1 = asyncio.create_task(p.execute(intent("a1"), lambda m: None))
    await asyncio.sleep(0.01)
    with pytest.raises(DuplicateExecution):
        await p.execute(intent("a2"), lambda m: None)
    await t1


def test_repeated_failed_transactions_trip_the_kill_switch():
    cfg = AppConfig()
    core = build_core(cfg, ManualClock(1.0), Mode.PAPER)
    for i in range(cfg.killswitch.max_execution_failures):
        rep = ExecutionReport(
            execution_id=f"f{i}",
            status=ExecStatus.FAILED,
            mint="M",
            side=Side.BUY,
            venue=Venue.BONDING_CURVE,
            requested_amount=1,
            error="slippage",
        )
        core._execution_safety(intent(f"f{i}"), rep)
    assert core.kill.state.killed


def test_uncertain_transaction_trips_kill_switch_immediately():
    core = build_core(AppConfig(), ManualClock(1.0), Mode.PAPER)
    rep = ExecutionReport(
        execution_id="u1",
        status=ExecStatus.UNCERTAIN,
        mint="M",
        side=Side.BUY,
        venue=Venue.BONDING_CURVE,
        requested_amount=1,
    )
    core._execution_safety(intent("u1"), rep)
    assert core.kill.state.killed and "UNCERTAIN" in core.kill.state.reason


def test_stale_market_data_blocks_entries(sched):
    cfg = AppConfig()
    tok = CurveToken("stale", t0=0).create().pump(1, 120, sol=0.3)
    clock = ManualClock(0)
    core = build_core(cfg, clock, Mode.PAPER)
    for ev in tok.events:
        clock.advance_to(ev.t)
        core.ingest(ev)
    clock.advance(30.0)  # nothing arrives for 30 s
    d = core.stage1(tok.mint, clock.now(), 0)
    assert d.gate("VALIDATE_DATA").status == "FAIL" and any("stale" in r for r in d.gate("VALIDATE_DATA").reasons)


def test_paper_fill_reverts_on_slippage_like_onchain(sched):
    from tradingagent.execution.providers import PaperFillModel
    from tradingagent.pump.adapters import BondingCurveAdapter
    from tradingagent.pump.curve import initial_state

    cfg = AppConfig().execution.model_copy(update={"paper_failure_rate": 0.0})
    a = BondingCurveAdapter(initial_state(creator=key("c")), sched)
    q = a.quote_buy(10**9)
    it = intent("s1")
    it.amount_in, it.min_out = 10**9, q.amount_out * 2  # impossible min_out
    rep = PaperFillModel(cfg).fill(it, a, 1.0)
    assert rep.status is ExecStatus.FAILED and "slippage" in rep.error and rep.network_fees > 0
