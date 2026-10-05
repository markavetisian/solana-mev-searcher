"""`ta` command-line interface.

ta config-check                    validate config, print configuration_version and LIVE readiness
ta migrate                         apply database migrations (alembic upgrade head)
ta worker [--source ws|jsonl|synthetic] [--input PATH]     run the trading/research runtime (mode from config)
ta api                             run the HTTP API
ta backtest  --input PATH | --db [--from TS] [--to TS] [--out FILE]
ta walkforward --input PATH | --db [--train-days N --test-days N --step-days N] [--out FILE]
ta research  --input PATH | --db [--out FILE] [--csv FILE]
ta synth --out FILE [--hours H] [--edge E] [--seed S]      SYNTHETIC data for tests/demos only
ta export-events --out FILE [--from TS] [--to TS]
ta status | ta pause | ta resume [--confirm RESET_KILL_SWITCH] | ta kill --reason R | ta paper-reset
ta security-check                  pre-live security review checklist
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from tradingagent.common.config import (
    LIVE_CONFIRM_PHRASE,
    AppConfig,
    LiveModeNotAuthorized,
    Secrets,
    live_guard,
    load_config,
    load_live_limits,
)
from tradingagent.common.logging import configure_logging
from tradingagent.common.types import Mode

DEFAULT_CONFIG = os.environ.get("TA_CONFIG", "config/default.yaml")


def _cfg(args: argparse.Namespace) -> AppConfig:
    paths = [DEFAULT_CONFIG] + (args.config or [])
    cfg = load_config(*paths)
    if getattr(args, "mode", None):
        cfg = cfg.with_overrides(mode=args.mode)
    return cfg


def _dump(obj: object, out: str | None) -> None:
    text = json.dumps(obj, indent=2, default=str)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text)
        print(f"wrote {out}")
    else:
        print(text)


async def _load_events(args: argparse.Namespace, cfg: AppConfig, secrets: Secrets) -> list:
    if args.input:
        from tradingagent.ingestion.sources import iter_jsonl

        evs = [
            e
            for e in iter_jsonl(args.input)
            if (args.start is None or e.t >= args.start) and (args.end is None or e.t < args.end)
        ]
        evs.sort(key=lambda e: e.order_key)
        return evs
    from tradingagent.storage.db import Database
    from tradingagent.storage.replay import load_db_events

    db = Database(secrets.database_url.get_secret_value())
    try:
        return await load_db_events(db, args.start, args.end, include_synthetic=args.allow_synthetic)
    finally:
        await db.close()


def _json_safe(obj: object) -> object:
    """JSON round-trip with NaN/inf -> None (PostgreSQL JSONB rejects non-finite numbers)."""
    import math

    def fix(x: object) -> object:
        if isinstance(x, float) and not math.isfinite(x):
            return None
        if isinstance(x, dict):
            return {str(k): fix(v) for k, v in x.items()}
        if isinstance(x, list | tuple):
            return [fix(v) for v in x]
        return x

    return fix(json.loads(json.dumps(obj, default=str)))


async def _save_run(secrets: Secrets, kind: str, params: dict, results: dict) -> None:
    from tradingagent.storage import models as m
    from tradingagent.storage.db import Database, DatabaseUnavailable

    db = Database(secrets.database_url.get_secret_value())
    try:
        table = m.ResearchExperiment.__table__ if kind == "research" else m.BacktestRun.__table__
        row = (
            {
                "name": "research_run",
                "params": params,
                "results": results,
                "data_range": results.get("data_range", {}),
                "versions": results.get("versions", {}),
            }
            if kind == "research"
            else {"kind": kind, "params": params, "results": results}
        )
        await db.write_now(table, _json_safe(row))
    except DatabaseUnavailable as e:
        print(f"warning: run not stored in database: {e}", file=sys.stderr)
    finally:
        await db.close()


# ---- commands -----------------------------------------------------------------------------------------------
def cmd_config_check(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    secrets = Secrets()
    print(f"mode: {cfg.mode.value}")
    print(f"configuration_version: {cfg.configuration_version}")
    checks = {
        "rpc_urls_configured": bool(secrets.rpc_urls()),
        "ws_urls_configured": bool(secrets.ws_urls()),
        "api_admin_token_set": bool(secrets.api_admin_token),
        "ai_enabled": cfg.ai.enabled,
        "ai_key_present": bool(secrets.anthropic_api_key),
        "telegram_configured": bool(secrets.telegram_bot_token and secrets.telegram_chat_id),
        "wallet_configured": bool(secrets.wallet_keypair_path or secrets.wallet_private_key_b58),
        "simulation_required": cfg.execution.simulation_required,
    }
    for k, v in checks.items():
        print(f"  {k:<28} {v}")
    if cfg.mode is Mode.LIVE:
        try:
            live_guard(cfg, secrets)
            print("LIVE guard: PASSED. Live limits:", load_live_limits().model_dump())
        except LiveModeNotAuthorized as e:
            print(f"LIVE guard: BLOCKED — {e}")
            return 2
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    return subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"])


def cmd_worker(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    configure_logging(cfg.log_level)
    from tradingagent.worker.factory import build_runtime

    async def main() -> None:
        rt = await build_runtime(cfg, Secrets(), args.source, args.input, use_db=not args.no_db)
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(rt.shutdown()))
        await rt.run()

    try:
        asyncio.run(main())
    except LiveModeNotAuthorized as e:
        print(f"refusing to start: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    configure_logging(cfg.log_level)
    import uvicorn

    from tradingagent.api.app import create_app

    uvicorn.run(
        create_app(cfg, Secrets()),
        host=args.host or cfg.api.host,
        port=args.port or cfg.api.port,
        log_level="warning",
        proxy_headers=False,
    )
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    configure_logging("WARNING")
    secrets = Secrets()
    from tradingagent.backtest.engine import Backtester
    from tradingagent.backtest.metrics import performance

    events = asyncio.run(_load_events(args, cfg, secrets))
    if not events:
        print("no events in range", file=sys.stderr)
        return 1
    t0 = time.time()
    res = Backtester(cfg, seed=args.seed, allow_synthetic=args.allow_synthetic or None, keep_decisions=50).run(events)
    report = {
        "kind": "backtest",
        "configuration_version": res.configuration_version,
        "synthetic_data": res.synthetic,
        "events": res.n_events,
        "tokens": res.n_tokens,
        "start": res.start,
        "end": res.end,
        "runtime_s": round(time.time() - t0, 1),
        "decisions": res.decisions,
        "gate_failures": res.rejections,
        "executions": res.executions,
        "kill_switch": res.killed,
        "metrics": performance(res.trade_dicts(), res.equity_curve, res.executions, cfg.backtest.bootstrap_samples),
        "calibration_outcomes": len(res.outcomes),
        "trades": res.trade_dicts(),
        "sample_decisions": res.sample_decisions,
        "warning": "In-sample backtest. Use `ta walkforward` for out-of-sample evaluation."
        + (" SYNTHETIC DATA: results say nothing about real edge." if res.synthetic else ""),
    }
    _dump(report, args.out)
    if not args.no_store:
        asyncio.run(_save_run(secrets, "backtest", vars_safe(args), {k: v for k, v in report.items() if k != "trades"}))
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    configure_logging("WARNING")
    secrets = Secrets()
    from tradingagent.backtest.walkforward import run_walkforward

    events = asyncio.run(_load_events(args, cfg, secrets))
    res = run_walkforward(
        cfg,
        events,
        args.train_days,
        args.test_days,
        args.step_days,
        args.seed,
        args.allow_synthetic or None,
        select=not args.no_select,
    )
    _dump(res, args.out)
    print("\nVERDICT:", res.get("verdict"))
    if not args.no_store:
        asyncio.run(_save_run(secrets, "walkforward", vars_safe(args), res))
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    configure_logging("WARNING")
    secrets = Secrets()
    from tradingagent.research.runner import run_research

    events = asyncio.run(_load_events(args, cfg, secrets))
    res = run_research(cfg, events, args.allow_synthetic or None)
    df = res.pop("_frame")
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"wrote labeled dataset {args.csv} ({len(df)} rows)")
    _dump(res, args.out)
    if not args.no_store:
        asyncio.run(_save_run(secrets, "research", vars_safe(args), res))
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    from tradingagent.ingestion.synthetic import SyntheticMarket

    evs = SyntheticMarket(seed=args.seed, planted_edge=args.edge, duration_s=args.hours * 3600).generate()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as fh:
        for e in evs:
            fh.write(json.dumps(e.to_dict(), separators=(",", ":")) + "\n")
    print(f"wrote {len(evs)} SYNTHETIC events to {args.out} (source=synthetic; not market data)")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from tradingagent.storage.db import Database
    from tradingagent.storage.replay import iter_db_events

    async def main() -> int:
        db = Database(Secrets().database_url.get_secret_value())
        n = 0
        with open(args.out, "w") as fh:
            async for e in iter_db_events(db, args.start, args.end, include_synthetic=True):
                fh.write(json.dumps(e.to_dict(), separators=(",", ":")) + "\n")
                n += 1
        await db.close()
        print(f"exported {n} events to {args.out}")
        return 0

    return asyncio.run(main())


def _command(name: str, args: argparse.Namespace) -> int:
    from tradingagent.storage import models as m
    from tradingagent.storage.db import Database

    async def main() -> int:
        db = Database(Secrets().database_url.get_secret_value())
        try:
            if name == "status":
                st, ts = await db.get_state("status")
                ks, _ = await db.get_state("killswitch")
                print(
                    json.dumps(
                        {"status": st, "heartbeat_age_s": (time.time() - ts) if ts else None, "killswitch": ks},
                        indent=2,
                        default=str,
                    )
                )
                return 0
            if name == "kill":
                ks, _ = await db.get_state("killswitch")
                latched = dict(ks or {})
                latched.update({"killed": True, "reason": args.reason, "source": "cli", "activated_at": time.time()})
                await db.set_state("killswitch", latched)
            await db.write_now(
                m.OperatorCommand.__table__,
                {
                    "t": time.time(),
                    "command": name.replace("-", "_"),
                    "operator": os.environ.get("USER", "cli"),
                    "args": {"reason": getattr(args, "reason", ""), "confirm": getattr(args, "confirm", "")},
                    "status": "PENDING",
                },
            )
            print(f"queued {name}")
            return 0
        finally:
            await db.close()

    return asyncio.run(main())


_SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "Anthropic API key"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), "Telegram bot token"),
    (re.compile(r"\[(\s*\d{1,3}\s*,){63}\s*\d{1,3}\s*\]"), "Solana keypair byte array"),
    (re.compile(r"api[-_]?key=[A-Za-z0-9\-]{16,}", re.I), "API key in URL"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key"),
]


def cmd_security_check(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    secrets = Secrets()
    problems: list[str] = []
    ok: list[str] = []
    try:
        tracked = subprocess.check_output(["git", "ls-files"], text=True).split()  # noqa: S607
    except (OSError, subprocess.CalledProcessError):
        tracked = [str(p) for p in Path(".").rglob("*") if p.is_file() and ".venv" not in p.parts]
    for f in tracked:
        p = Path(f)
        if not p.is_file() or p.stat().st_size > 2_000_000 or p.suffix in (".png", ".jpg", ".ico", ".lock"):
            continue
        try:
            txt = p.read_text(errors="ignore")
        except OSError:
            continue
        for rx, what in _SECRET_PATTERNS:
            if rx.search(txt):
                problems.append(f"possible {what} committed in {f}")
        if p.name in (".env", "id.json") or p.name.endswith((".keypair.json", "-keypair.json")):
            problems.append(f"secret-bearing file tracked by git: {f}")
    env = Path(".env")
    if env.exists() and stat.S_IMODE(env.stat().st_mode) & 0o077:
        problems.append(".env is readable by group/others (chmod 600 .env)")
    if secrets.wallet_keypair_path:
        kp = Path(secrets.wallet_keypair_path)
        if kp.exists() and stat.S_IMODE(kp.stat().st_mode) & 0o077:
            problems.append("wallet keypair file permissions too open (chmod 600)")
        else:
            ok.append("wallet keypair file permissions")
    if not secrets.api_admin_token:
        problems.append("API_ADMIN_TOKEN not set: control endpoints disabled (required for LIVE)")
    elif len(secrets.api_admin_token.get_secret_value()) < 32:
        problems.append("API_ADMIN_TOKEN shorter than 32 characters")
    if not cfg.api.require_auth_for_reads:
        problems.append("api.require_auth_for_reads=false: dashboard data is public")
    if any(o == "*" for o in cfg.api.cors_origins):
        problems.append("CORS allows any origin")
    if cfg.api.host not in ("127.0.0.1", "localhost"):
        ok.append(f"API bound to {cfg.api.host}: ensure it sits behind TLS + an authenticating reverse proxy")
    if not cfg.execution.simulation_required:
        problems.append("execution.simulation_required=false")
    if cfg.mode is Mode.LIVE and secrets.live_trading_confirm != LIVE_CONFIRM_PHRASE:
        problems.append("mode LIVE without LIVE_TRADING_CONFIRM")
    if cfg.ai.enabled and cfg.ai.required_for_entry:
        ok.append("note: ai.required_for_entry=true makes AI availability a hard dependency for entries")
    print("SECURITY CHECK")
    for x in ok:
        print("  ok/info:", x)
    for x in problems:
        print("  PROBLEM:", x)
    print("RESULT:", "FAIL" if problems else "PASS")
    return 1 if problems else 0


def vars_safe(args: argparse.Namespace) -> dict:
    return {k: v for k, v in vars(args).items() if k not in ("func",)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ta", description="Pump.fun quantitative research & execution system")
    p.add_argument("--config", action="append", help="extra YAML config file(s) merged over config/default.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    def data_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--input", help="JSONL file or directory of recorded events")
        sp.add_argument("--db", action="store_true", help="load events from the database (default if no --input)")
        sp.add_argument("--from", dest="start", type=float)
        sp.add_argument("--to", dest="end", type=float)
        sp.add_argument("--allow-synthetic", action="store_true")
        sp.add_argument("--seed", type=int, default=0)
        sp.add_argument("--out")
        sp.add_argument("--no-store", action="store_true", help="do not record the run in the database")

    s = sub.add_parser("config-check")
    s.add_argument("--mode", choices=[m.value for m in Mode])
    s.set_defaults(func=cmd_config_check)
    sub.add_parser("migrate").set_defaults(func=cmd_migrate)
    s = sub.add_parser("worker")
    s.add_argument("--source", choices=["ws", "jsonl", "synthetic"], default="ws")
    s.add_argument("--input")
    s.add_argument("--mode", choices=[m.value for m in Mode])
    s.add_argument("--no-db", action="store_true")
    s.set_defaults(func=cmd_worker)
    s = sub.add_parser("api")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_api)
    s = sub.add_parser("backtest")
    data_args(s)
    s.set_defaults(func=cmd_backtest)
    s = sub.add_parser("walkforward")
    data_args(s)
    s.add_argument("--train-days", type=float)
    s.add_argument("--test-days", type=float)
    s.add_argument("--step-days", type=float)
    s.add_argument("--no-select", action="store_true", help="no threshold selection on training folds")
    s.set_defaults(func=cmd_walkforward)
    s = sub.add_parser("research")
    data_args(s)
    s.add_argument("--csv", help="write the labeled research dataset")
    s.set_defaults(func=cmd_research)
    s = sub.add_parser("synth")
    s.add_argument("--out", required=True)
    s.add_argument("--hours", type=float, default=2.0)
    s.add_argument("--edge", type=float, default=0.0)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(func=cmd_synth)
    s = sub.add_parser("export-events")
    s.add_argument("--out", required=True)
    s.add_argument("--from", dest="start", type=float)
    s.add_argument("--to", dest="end", type=float)
    s.set_defaults(func=cmd_export)
    for name in ("status", "pause", "resume", "kill", "paper-reset"):
        s = sub.add_parser(name)
        s.add_argument("--reason", default=f"cli {name}")
        s.add_argument("--confirm", default="")
        s.set_defaults(func=lambda a, n=name: _command(n, a))
    sub.add_parser("security-check").set_defaults(func=cmd_security_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
