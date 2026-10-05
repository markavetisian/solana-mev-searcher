"""Assemble a runtime from configuration + secrets. Live execution components are only constructed in LIVE mode
(or SHADOW with a wallet, for simulateTransaction), and only after `live_guard` has passed."""

from __future__ import annotations

from tradingagent.ai.analyst import build_analyst
from tradingagent.alerts.telegram import LogAlertSink, TelegramAlertSink
from tradingagent.common.config import AppConfig, Secrets, live_guard, load_live_limits
from tradingagent.common.logging import get_logger
from tradingagent.common.types import Mode
from tradingagent.execution.providers import (
    ChainContext,
    ExecutionProvider,
    LiveExecutionProvider,
    PaperExecutionProvider,
    ShadowExecutionProvider,
)
from tradingagent.ingestion.parser import LogParser
from tradingagent.ingestion.sources import EventSource, JsonlReplaySource, ListSource, WebSocketLogSource
from tradingagent.paper.runtime import TradingRuntime
from tradingagent.solana.rpc import RpcPool
from tradingagent.solana.tx import TransactionEngine, TxRecord
from tradingagent.solana.wallet import WalletError, load_keypair
from tradingagent.storage import models as m
from tradingagent.storage.db import Database

log = get_logger("worker.factory")


def build_source(
    cfg: AppConfig, secrets: Secrets, kind: str, path: str | None = None, pool_to_mint: dict[str, str] | None = None
) -> EventSource:
    if kind == "ws":
        urls = secrets.ws_urls()
        if not urls:
            raise SystemExit("SOLANA_WS_URLS is not set (required for live ingestion)")
        s = cfg.solana
        return WebSocketLogSource(
            urls,
            LogParser(pool_to_mint),
            s.commitment,
            s.subscribe_pumpswap,
            s.ws_stale_after_s,
            s.ws_ping_interval_s,
            s.ws_reconnect_max_backoff_s,
        )
    if kind == "jsonl":
        if not path:
            raise SystemExit("--input is required for --source jsonl")
        return JsonlReplaySource(path)
    if kind == "synthetic":
        from tradingagent.ingestion.synthetic import SyntheticMarket

        log.warning("synthetic_source", detail="SYNTHETIC DATA: demo/testing only, says nothing about real edge")
        return ListSource(SyntheticMarket(seed=11, duration_s=3600, start_time=__import__("time").time()).generate())
    raise SystemExit(f"unknown source {kind}")


async def build_runtime(
    cfg: AppConfig, secrets: Secrets, source_kind: str = "ws", input_path: str | None = None, use_db: bool = True
) -> TradingRuntime:
    live_guard(cfg, secrets)
    mode = cfg.mode
    db = Database(secrets.database_url.get_secret_value()) if use_db else None
    if mode is Mode.LIVE and db is None:
        raise SystemExit("LIVE mode requires a database")
    rpc = (
        RpcPool(
            secrets.rpc_urls(),
            cfg.solana.rpc_timeout_s,
            cfg.solana.rpc_max_retries_per_call,
            cfg.solana.rpc_circuit_open_after_errors,
            cfg.solana.rpc_circuit_cooldown_s,
        )
        if secrets.rpc_urls()
        else None
    )
    pool_to_mint: dict[str, str] = {}
    source = build_source(cfg, secrets, source_kind, input_path, pool_to_mint)
    provider: ExecutionProvider | None = None
    wallet_pubkey = None
    live_limits = load_live_limits() if mode is Mode.LIVE else None
    if mode is Mode.PAPER:
        provider = PaperExecutionProvider(cfg.execution)
    elif mode is Mode.SHADOW:
        ctx = None
        if rpc and (secrets.wallet_keypair_path or secrets.wallet_private_key_b58):
            try:
                kp = load_keypair(secrets)
                ctx, wallet_pubkey = ChainContext(rpc=rpc, keypair=kp), str(kp.pubkey())
            except WalletError as e:
                log.warning("shadow_without_wallet", error=str(e))
        provider = ShadowExecutionProvider(cfg.execution, ctx=ctx)
    elif mode is Mode.LIVE:
        assert rpc is not None and db is not None and live_limits is not None
        kp = load_keypair(secrets)
        wallet_pubkey = str(kp.pubkey())

        async def persist(rec: TxRecord) -> None:
            await db.write_now(
                m.Execution.__table__,
                {
                    "execution_id": rec.execution_id,
                    "status": rec.status.value,
                    "transaction_signature": rec.signature,
                    "blockhash": rec.blockhash,
                    "last_valid_block_height": rec.last_valid_block_height,
                    "slot": rec.slot,
                    "requested_amount": 0,
                    "expected_price": 0.0,
                    "error": rec.error,
                    "submitted_at": rec.submitted_at,
                    "completed_at": rec.confirmed_at,
                    "extra": {"tx": rec.to_dict()},
                },
                upsert_key="execution_id",
            )
            await db.write_now(
                m.ExecutionEvent.__table__,
                {
                    "execution_id": rec.execution_id,
                    "t": __import__("time").time(),
                    "status": rec.status.value,
                    "detail": rec.to_dict(),
                },
            )

        engine = TransactionEngine(rpc, kp, cfg.execution, on_update=persist)
        provider = LiveExecutionProvider(cfg.execution, live_limits, ChainContext(rpc=rpc, keypair=kp), engine)
        log.critical("LIVE_MODE_ARMED", wallet=wallet_pubkey, limits=live_limits.model_dump())
    analyst = build_analyst(cfg.ai, secrets.anthropic_api_key.get_secret_value() if secrets.anthropic_api_key else None)
    alerts = LogAlertSink()
    if cfg.alerts.telegram_enabled and secrets.telegram_bot_token and secrets.telegram_chat_id:
        alerts = TelegramAlertSink(
            secrets.telegram_bot_token.get_secret_value(),
            secrets.telegram_chat_id.get_secret_value(),
            cfg.alerts.max_messages_per_minute,
        )
    return TradingRuntime(cfg, mode, source, provider, db, analyst, alerts, rpc, live_limits, wallet_pubkey)
