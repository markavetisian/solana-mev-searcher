"""Backend API. Reads state published by the worker and the audit tables; writes only operator commands.

Security:
  * Bearer-token auth (constant-time compare). Read token for GETs (optional via api.require_auth_for_reads),
    admin token for every POST. No token configured => POST endpoints are disabled entirely.
  * There is NO endpoint that signs, builds or submits transactions, exposes keys, or changes risk limits.
  * Per-client rate limiting; restrictive CORS; untrusted token metadata is returned as data only.
  * POST /system/kill also latches the persisted kill state directly, so it works even if the worker is down.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from tradingagent.common.config import AppConfig, Secrets
from tradingagent.common.types import Mode
from tradingagent.risk.killswitch import RESET_PHRASE
from tradingagent.storage import models as m
from tradingagent.storage.db import Database, DatabaseUnavailable
from tradingagent.storage.sink import row_to_event


class CommandBody(BaseModel):
    reason: str = Field(default="", max_length=300)
    operator: str = Field(default="operator", max_length=64, pattern=r"^[\w.@-]+$")
    confirm: str = Field(default="", max_length=64)


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        if len(self.hits) > 10_000:
            self.hits.clear()
        return True


def _token(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else ""


def _eq(a: str, b: str | None) -> bool:
    return bool(b) and hmac.compare_digest(a.encode(), b.encode())  # type: ignore[union-attr]


def create_app(cfg: AppConfig, secrets: Secrets, db: Database | None = None) -> FastAPI:
    db = db or Database(secrets.database_url.get_secret_value())
    admin = secrets.api_admin_token.get_secret_value() if secrets.api_admin_token else None
    reader = secrets.api_read_token.get_secret_value() if secrets.api_read_token else None
    limiter = RateLimiter(cfg.api.rate_limit_per_minute)
    app = FastAPI(title="Trading Agent API", version="0.1.0", docs_url="/docs", redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.api.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.state.db, app.state.cfg = db, cfg

    @app.middleware("http")
    async def _limit(request: Request, call_next):
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return JSONResponse({"detail": "rate limited"}, status_code=429)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def require_read(request: Request) -> None:
        if not cfg.api.require_auth_for_reads:
            return
        tok = _token(request)
        if not (_eq(tok, reader) or _eq(tok, admin)):
            raise HTTPException(401, "unauthorized")

    def require_admin(request: Request) -> str:
        if not admin:
            raise HTTPException(403, "control endpoints disabled: API_ADMIN_TOKEN not configured")
        if not _eq(_token(request), admin):
            raise HTTPException(401, "unauthorized")
        return "admin"

    async def state(key: str) -> tuple[dict | None, float | None]:
        try:
            return await db.get_state(key)
        except DatabaseUnavailable as e:
            raise HTTPException(503, f"database unavailable: {str(e)[:120]}") from e

    async def worker_status() -> dict:
        st, ts = await state("status")
        if not st:
            return {"worker": "NOT_RUNNING", "mode": cfg.mode.value}
        st = dict(st)
        st["worker"] = "STALE" if (ts is None or time.time() - ts > cfg.api.worker_heartbeat_stale_s) else "RUNNING"
        st["heartbeat_age_s"] = (time.time() - ts) if ts else None
        return st

    # ---- health / status --------------------------------------------------------------------------------------
    @app.get("/health")
    async def health() -> dict:
        ok = await db.ping()
        st, ts = (await db.get_state("status")) if ok else (None, None)
        return {
            "status": "ok" if ok else "degraded",
            "database": ok,
            "mode": (st or {}).get("mode", cfg.mode.value),
            "worker_heartbeat_age_s": (time.time() - ts) if ts else None,
        }

    @app.get("/system/status", dependencies=[Depends(require_read)])
    async def system_status() -> dict:
        st = await worker_status()
        ks, _ = await state("killswitch")
        st["kill_switch_persisted"] = ks
        st["live_enabled"] = st.get("mode") == Mode.LIVE.value
        return st

    # ---- market ----------------------------------------------------------------------------------------------
    @app.get("/tokens", dependencies=[Depends(require_read)])
    async def tokens(state_: str | None = Query(None, alias="state"), limit: int = Query(100, le=500)) -> dict:
        async with db.session() as s:
            q = select(m.Token).order_by(desc(m.Token.last_trade_at)).limit(limit)
            if state_:
                q = q.where(m.Token.market_state == state_)
            rows = (await s.execute(q)).scalars().all()
        return {"tokens": [_token_dict(r) for r in rows]}

    @app.get("/tokens/{mint}", dependencies=[Depends(require_read)])
    async def token_detail(mint: str, window_s: int = Query(3600, le=86_400)) -> dict:
        if not (32 <= len(mint) <= 44) or not mint.isalnum():
            raise HTTPException(400, "invalid mint")
        since = time.time() - window_s
        async with db.session() as s:
            tok = (await s.execute(select(m.Token).where(m.Token.mint == mint))).scalar_one_or_none()
            evs = (
                (
                    await s.execute(
                        select(m.MarketEventRow)
                        .where(m.MarketEventRow.mint == mint, m.MarketEventRow.observed_at >= since)
                        .order_by(m.MarketEventRow.observed_at)
                        .limit(5000)
                    )
                )
                .scalars()
                .all()
            )
            snaps = (
                (
                    await s.execute(
                        select(m.MarketSnapshot)
                        .where(m.MarketSnapshot.mint == mint, m.MarketSnapshot.t >= since)
                        .order_by(m.MarketSnapshot.t)
                        .limit(2000)
                    )
                )
                .scalars()
                .all()
            )
            sigs = (
                (await s.execute(select(m.Signal).where(m.Signal.mint == mint).order_by(desc(m.Signal.t)).limit(20)))
                .scalars()
                .all()
            )
            ai = (
                (
                    await s.execute(
                        select(m.AIAnalysisRow)
                        .where(m.AIAnalysisRow.mint == mint)
                        .order_by(desc(m.AIAnalysisRow.t))
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            feats = (
                (
                    await s.execute(
                        select(m.FeatureRow).where(m.FeatureRow.mint == mint).order_by(desc(m.FeatureRow.t)).limit(1)
                    )
                )
                .scalars()
                .all()
            )
        trades = []
        holders: dict[str, int] = {}
        for r in evs:
            ev = row_to_event(r)
            if ev.kind.value in ("TRADE", "AMM_TRADE"):
                px = ev.price_after
                trades.append(
                    {
                        "t": ev.observed_at,
                        "side": ev.side.value if ev.side else None,
                        "sol": ev.sol_amount / 1e9,
                        "tokens": ev.token_amount,
                        "price": px,
                        "user": ev.user,
                        "venue": ev.venue.value if ev.venue else None,
                    }
                )
                if ev.user:
                    holders[ev.user] = holders.get(ev.user, 0) + (
                        ev.token_amount if ev.side and ev.side.value == "BUY" else -ev.token_amount
                    )
        top = sorted(((u, b) for u, b in holders.items() if b > 0), key=lambda x: -x[1])[:20]
        return {
            "token": _token_dict(tok) if tok else {"mint": mint},
            "trades": trades,
            "snapshots": [
                {
                    "t": x.t,
                    "price": x.price_lamports_per_token,
                    "market_cap_sol": x.market_cap_sol,
                    "liquidity_sol": x.liquidity_sol,
                    "volume_sol_5m": x.volume_sol_5m,
                    "holders": x.holders,
                    "progress": x.progress,
                    "state": x.state,
                }
                for x in snaps
            ],
            "holder_distribution_window": [{"wallet": u, "tokens": b, "share": b / 1e15} for u, b in top],
            "decisions": [_signal_dict(x) for x in sigs],
            "ai_analyses": [
                {
                    "t": a.t,
                    "status": a.status,
                    "model": a.model,
                    "assessment": a.assessment,
                    "error": a.error,
                    "latency_ms": a.latency_ms,
                }
                for a in ai
            ],
            "latest_features": feats[0].values if feats else None,
            "note": "name/symbol/uri are untrusted third-party text",
        }

    @app.get("/opportunities", dependencies=[Depends(require_read)])
    async def opportunities() -> dict:
        st, ts = await state("opportunities")
        return {"rows": (st or {}).get("rows", []), "updated_at": ts}

    # ---- portfolio / trades ------------------------------------------------------------------------------------
    async def _portfolio() -> dict:
        st = await worker_status()
        mode = st.get("mode", cfg.mode.value)
        p, ts = await state(f"portfolio:{mode}")
        return {"mode": mode, "updated_at": ts, **(p or {"snapshot": None, "positions": [], "recent_trades": []})}

    @app.get("/positions", dependencies=[Depends(require_read)])
    async def positions() -> dict:
        p = await _portfolio()
        return {"mode": p["mode"], "positions": p.get("positions", [])}

    @app.get("/positions/{position_id}", dependencies=[Depends(require_read)])
    async def position_detail(position_id: str) -> dict:
        p = await _portfolio()
        for pos in p.get("positions", []):
            if pos["position_id"] == position_id:
                return {"position": pos, "open": True}
        async with db.session() as s:
            t = (await s.execute(select(m.TradeRow).where(m.TradeRow.position_id == position_id))).scalar_one_or_none()
            evs = (
                (
                    await s.execute(
                        select(m.PositionEvent)
                        .where(m.PositionEvent.position_id == position_id)
                        .order_by(m.PositionEvent.t)
                    )
                )
                .scalars()
                .all()
            )
        if not t and not evs:
            raise HTTPException(404, "position not found")
        return {
            "open": False,
            "trade": t.data if t else None,
            "events": [{"t": e.t, "event": e.event, "detail": e.detail} for e in evs],
        }

    @app.get("/portfolio", dependencies=[Depends(require_read)])
    async def portfolio() -> dict:
        p = await _portfolio()
        async with db.session() as s:
            snaps = (
                (
                    await s.execute(
                        select(m.PortfolioSnapshot)
                        .where(m.PortfolioSnapshot.mode == p["mode"])
                        .order_by(desc(m.PortfolioSnapshot.t))
                        .limit(1440)
                    )
                )
                .scalars()
                .all()
            )
        p["equity_curve"] = [{"t": x.t, "equity_sol": x.equity_sol, "drawdown": x.drawdown} for x in reversed(snaps)]
        return p

    @app.get("/trades", dependencies=[Depends(require_read)])
    async def trades(limit: int = Query(200, le=2000), mode: str | None = None) -> dict:
        from tradingagent.backtest.metrics import performance

        async with db.session() as s:
            q = select(m.TradeRow).order_by(desc(m.TradeRow.closed_at)).limit(limit)
            if mode:
                q = q.where(m.TradeRow.mode == mode)
            rows = (await s.execute(q)).scalars().all()
        data = [r.data for r in rows]
        return {"trades": data, "metrics": performance(data, None, None, 500)}

    @app.get("/risk", dependencies=[Depends(require_read)])
    async def risk() -> dict:
        ks, _ = await state("killswitch")
        async with db.session() as s:
            evs = (await s.execute(select(m.RiskEvent).order_by(desc(m.RiskEvent.t)).limit(100))).scalars().all()
            rej = (
                await s.execute(
                    select(m.Signal.outcome, func.count())
                    .where(m.Signal.t >= time.time() - 3600)
                    .group_by(m.Signal.outcome)
                )
            ).all()
        return {
            "kill_switch": ks,
            "limits": cfg.risk.model_dump(),
            "filters": cfg.filters.model_dump(),
            "decisions_last_hour": {o: n for o, n in rej},
            "risk_events": [{"t": e.t, "kind": e.kind, "severity": e.severity, "detail": e.detail} for e in evs],
        }

    @app.get("/decisions", dependencies=[Depends(require_read)])
    async def decisions(outcome: str | None = None, limit: int = Query(100, le=1000)) -> dict:
        async with db.session() as s:
            q = select(m.Signal).order_by(desc(m.Signal.t)).limit(limit)
            if outcome:
                q = q.where(m.Signal.outcome == outcome)
            rows = (await s.execute(q)).scalars().all()
        return {"decisions": [_signal_dict(x) for x in rows]}

    @app.get("/metrics", dependencies=[Depends(require_read)])
    async def metrics(format: str = "json"):
        snap, ts = await state("metrics")
        if format == "prometheus":
            lines = []
            for k, v in ((snap or {}).get("counters") or {}).items():
                n = "ta_" + "".join(c if c.isalnum() else "_" for c in k)
                lines.append(f"{n}_total {v}")
            for k, v in ((snap or {}).get("gauges") or {}).items():
                n = "ta_" + "".join(c if c.isalnum() else "_" for c in k)
                lines.append(f"{n} {v}")
            return PlainTextResponse("\n".join(lines) + "\n")
        return {"metrics": snap, "updated_at": ts}

    @app.get("/research/experiments", dependencies=[Depends(require_read)])
    async def experiments(limit: int = Query(20, le=200)) -> dict:
        async with db.session() as s:
            rows = (
                (await s.execute(select(m.ResearchExperiment).order_by(desc(m.ResearchExperiment.id)).limit(limit)))
                .scalars()
                .all()
            )
        return {
            "experiments": [
                {
                    "id": r.id,
                    "name": r.name,
                    "created_at": r.created_at.isoformat(),
                    "params": r.params,
                    "results": r.results,
                    "data_range": r.data_range,
                    "versions": r.versions,
                }
                for r in rows
            ]
        }

    @app.get("/backtests", dependencies=[Depends(require_read)])
    async def backtests(limit: int = Query(20, le=200)) -> dict:
        async with db.session() as s:
            rows = (
                (await s.execute(select(m.BacktestRun).order_by(desc(m.BacktestRun.id)).limit(limit))).scalars().all()
            )
        return {
            "runs": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat(),
                    "kind": r.kind,
                    "params": r.params,
                    "results": r.results,
                }
                for r in rows
            ]
        }

    @app.get("/stream", dependencies=[Depends(require_read)])
    async def stream(request: Request) -> StreamingResponse:
        async def gen():
            while not await request.is_disconnected():
                try:
                    st = await worker_status()
                    p = await _portfolio()
                    o, _ = await db.get_state("opportunities")
                    payload = {
                        "status": st,
                        "portfolio": p.get("snapshot"),
                        "positions": p.get("positions", []),
                        "opportunities": (o or {}).get("rows", [])[:25],
                    }
                    yield f"data: {json.dumps(payload, default=str)}\n\n"
                except (DatabaseUnavailable, HTTPException):
                    yield 'data: {"error":"database unavailable"}\n\n'
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---- control (admin) ----------------------------------------------------------------------------------------
    async def enqueue(command: str, body: CommandBody) -> dict:
        try:
            await db.write_now(
                m.OperatorCommand.__table__,
                {
                    "t": time.time(),
                    "command": command,
                    "args": {"reason": body.reason, "confirm": body.confirm},
                    "operator": body.operator,
                    "status": "PENDING",
                },
            )
        except DatabaseUnavailable as e:
            raise HTTPException(503, "database unavailable; command not recorded") from e
        return {"queued": command}

    @app.post("/system/pause", dependencies=[Depends(require_admin)])
    async def pause(body: CommandBody) -> dict:
        return await enqueue("pause", body)

    @app.post("/system/resume", dependencies=[Depends(require_admin)])
    async def resume(body: CommandBody) -> dict:
        ks, _ = await state("killswitch")
        if ks and ks.get("killed") and body.confirm != RESET_PHRASE:
            raise HTTPException(409, f"kill switch latched: resume requires confirm='{RESET_PHRASE}'")
        return await enqueue("resume", body)

    @app.post("/system/kill", dependencies=[Depends(require_admin)])
    async def kill(body: CommandBody) -> dict:
        reason = body.reason or "operator kill via API"
        ks, _ = await state("killswitch")
        latched = dict(ks or {})
        latched.update(
            {"killed": True, "reason": reason, "source": f"api:{body.operator}", "activated_at": time.time()}
        )
        try:
            await db.set_state("killswitch", latched)  # durable even if the worker is down
        except DatabaseUnavailable as e:
            raise HTTPException(503, "database unavailable") from e
        await enqueue("kill", body)
        return {"killed": True, "reason": reason}

    @app.post("/paper/reset", dependencies=[Depends(require_admin)])
    async def paper_reset(body: CommandBody) -> dict:
        st = await worker_status()
        if st.get("mode") == Mode.LIVE.value:
            raise HTTPException(409, "paper reset is not available in LIVE mode")
        return await enqueue("paper_reset", body)

    return app


def _token_dict(t: Any) -> dict:
    return {
        "mint": t.mint,
        "name": t.name,
        "symbol": t.symbol,
        "uri": t.uri,
        "creator": t.creator,
        "created_at": t.created_at,
        "market_state": t.market_state,
        "pool_address": t.pool_address,
        "bonding_curve_address": t.bonding_curve_address,
        "graduation_state": t.graduation_state,
        "first_seen_slot": t.first_seen_slot,
        "last_seen_slot": t.last_seen_slot,
        "last_trade_at": t.last_trade_at,
        "partial_history": t.partial_history,
    }


def _signal_dict(x: Any) -> dict:
    return {
        "decision_id": x.decision_id,
        "mint": x.mint,
        "symbol": x.symbol,
        "t": x.t,
        "mode": x.mode,
        "outcome": x.outcome,
        "market_state": x.market_state,
        "regime": x.regime,
        "score": x.score_total,
        "ev": x.ev,
        "sizing": x.sizing,
        "execution_estimate": x.execution_estimate,
        "gates": x.gates,
        "rejection_reasons": x.rejection_reasons,
        "versions": x.versions,
    }
