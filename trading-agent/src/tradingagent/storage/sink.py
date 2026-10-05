"""DbSink: maps TradingCore audit callbacks onto the append-only tables (via the BatchWriter)."""

from __future__ import annotations

import time
from typing import Any

from tradingagent.common.events import MarketEvent
from tradingagent.common.types import Mode
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.discovery.registry import TokenRecord
from tradingagent.execution.orders import ExecutionReport, OrderIntent
from tradingagent.portfolio.portfolio import ClosedTrade, Position
from tradingagent.storage import models as m
from tradingagent.storage.db import BatchWriter
from tradingagent.strategy.decision import Decision
from tradingagent.strategy.ev import OutcomeSample

_EVENT_COLS = {
    "kind",
    "mint",
    "signature",
    "slot",
    "seq",
    "observed_at",
    "chain_time",
    "venue",
    "side",
    "user",
    "sol_amount",
    "token_amount",
    "source",
}


def event_row(ev: MarketEvent) -> dict:
    d = ev.to_dict()
    row = {k: d[k] for k in _EVENT_COLS}
    row["payload"] = {k: v for k, v in d.items() if k not in _EVENT_COLS and v not in (None, {}, 0, False)}
    return row


def row_to_event(row: Any) -> MarketEvent:
    d = {k: getattr(row, k) for k in _EVENT_COLS}
    d.update(row.payload or {})
    return MarketEvent.from_dict(d)


class DbSink:
    def __init__(self, writer: BatchWriter, mode: Mode, session_id: str, persist_features: bool = True) -> None:
        self.w, self.mode, self.session_id, self.persist_features = writer, mode, session_id, persist_features

    # market data
    def market_event(self, ev: MarketEvent) -> None:
        self.w.put(m.MarketEventRow.__table__, event_row(ev), ignore_conflicts=True)

    def snapshot(self, snap: dict) -> None:
        self.w.put(
            m.MarketSnapshot.__table__,
            {
                "mint": snap["mint"],
                "t": snap["t"],
                "state": snap["state"],
                "venue": snap["venue"],
                "price_lamports_per_token": snap["price_lamports_per_token"],
                "market_cap_sol": snap["market_cap_sol"],
                "liquidity_sol": snap["liquidity_sol"],
                "volume_sol_5m": snap["volume_sol_5m"],
                "buys_5m": snap["buys_5m"],
                "sells_5m": snap["sells_5m"],
                "holders": snap["holders"],
                "progress": snap["progress"],
                "payload": snap,
            },
        )

    def transition(self, rec: TokenRecord, old: str, new: str, reason: str, t: float) -> None:
        self.w.put(
            m.TokenStateTransition.__table__,
            {"mint": rec.mint, "t": t, "from_state": old, "to_state": new, "reason": reason},
        )

    # decisions
    def decision(self, d: Decision) -> None:
        v = d.versions
        self.w.put(
            m.Signal.__table__,
            {
                "decision_id": d.decision_id,
                "mint": d.mint,
                "symbol": d.symbol,
                "t": d.t,
                "mode": self.mode.value,
                "session_id": self.session_id,
                "outcome": d.outcome.value,
                "market_state": d.market_state,
                "regime": d.regime.regime.value if d.regime else None,
                "score_total": d.score.total if d.score else None,
                "ev": d.ev.to_dict() if d.ev else None,
                "sizing": d.sizing.to_dict() if d.sizing else None,
                "execution_estimate": d.execution.to_dict() if d.execution else None,
                "gates": [g.to_dict() for g in d.gates],
                "rejection_reasons": d.rejection_reasons,
                "strategy_version": v["strategy_version"],
                "feature_version": v["feature_version"],
                "configuration_version": v["configuration_version"],
                "model_version": v["model_version"],
                "ai_model": v["ai_model"],
                "versions": v,
            },
        )
        if d.features is not None and self.persist_features:
            self.w.put(
                m.FeatureRow.__table__,
                {
                    "decision_id": d.decision_id,
                    "mint": d.mint,
                    "t": d.t,
                    "version": d.features.version,
                    "values": d.features.values,
                },
            )
        if d.score is not None:
            self.w.put(
                m.ScoreRow.__table__,
                {
                    "decision_id": d.decision_id,
                    "mint": d.mint,
                    "t": d.t,
                    "version": d.score.version,
                    "total": d.score.total,
                    "components": d.score.to_dict()["components"],
                },
            )
        if d.ai is not None and d.ai.status != "DISABLED":
            a = d.ai
            self.w.put(
                m.AIAnalysisRow.__table__,
                {
                    "decision_id": d.decision_id,
                    "mint": d.mint,
                    "t": d.t,
                    "status": a.status,
                    "model": a.model,
                    "prompt_version": a.prompt_version,
                    "latency_ms": a.latency_ms,
                    "assessment": a.assessment.model_dump() if a.assessment else None,
                    "error": a.error,
                    "input_hash": a.input_hash,
                },
            )

    # orders / executions
    def order_row(self, intent: OrderIntent, versions: dict) -> dict:
        return {
            "execution_id": intent.execution_id,
            "decision_id": intent.decision_id,
            "position_id": intent.position_id,
            "session_id": self.session_id,
            "mode": self.mode.value,
            "mint": intent.mint,
            "side": intent.side.value,
            "venue": intent.venue.value,
            "amount_in": intent.amount_in,
            "min_out": intent.min_out,
            "expected_out": intent.expected_out,
            "expected_price": intent.expected_price,
            "spot_price": intent.spot_price,
            "expected_slippage": intent.expected_slippage,
            "priority_micro_lamports": intent.priority_micro_lamports,
            "reason": intent.reason,
            "created_at": intent.created_at,
            "versions": versions,
        }

    def execution(self, intent: OrderIntent, rep: ExecutionReport) -> None:
        # The mutable `executions` row is upserted synchronously by the runtime; here only the immutable log.
        self.w.put(
            m.ExecutionEvent.__table__,
            {"execution_id": rep.execution_id, "t": time.time(), "status": rep.status.value, "detail": rep.to_dict()},
        )

    def position(self, pos: Position, event: str, detail: dict | None = None) -> None:
        self.w.put(
            m.PositionEvent.__table__,
            {
                "position_id": pos.position_id,
                "t": time.time(),
                "event": event,
                "detail": {"position": pos.to_dict(), **(detail or {})},
            },
        )

    def trade(self, ct: ClosedTrade) -> None:
        self.w.put(
            m.TradeRow.__table__,
            {
                "position_id": ct.position_id,
                "session_id": self.session_id,
                "mode": self.mode.value,
                "mint": ct.mint,
                "opened_at": ct.opened_at,
                "closed_at": ct.closed_at,
                "pnl_sol": ct.pnl_lamports / LAMPORTS_PER_SOL,
                "net_return": ct.net_return,
                "exit_reason": ct.exit_reason,
                "strategy_version": ct.versions.get("strategy_version", ""),
                "configuration_version": ct.versions.get("configuration_version", ""),
                "data": ct.to_dict(),
            },
            ignore_conflicts=True,
        )

    def risk_event(self, kind: str, detail: dict, severity: str = "WARNING") -> None:
        self.w.put(m.RiskEvent.__table__, {"t": time.time(), "kind": kind, "severity": severity, "detail": detail})

    def system_event(self, component: str, event: str, severity: str, detail: dict) -> None:
        self.w.put(
            m.SystemEvent.__table__,
            {"t": time.time(), "component": component, "event": event, "severity": severity, "detail": detail},
        )

    def outcome(self, s: OutcomeSample) -> None:
        self.w.put(m.OutcomeSampleRow.__table__, dict(s.__dict__))

    def portfolio_snapshot(self, snap: dict) -> None:
        self.w.put(
            m.PortfolioSnapshot.__table__,
            {
                "session_id": self.session_id,
                "mode": self.mode.value,
                "t": snap["t"],
                "equity_sol": snap["equity_sol"],
                "cash_sol": snap["cash_sol"],
                "realized_pnl_sol": snap["realized_pnl_sol"],
                "unrealized_pnl_sol": snap["unrealized_pnl_sol"],
                "drawdown": snap["drawdown"],
                "open_positions": snap["open_positions"],
                "data": snap,
            },
        )


def execution_row(rep: ExecutionReport) -> dict:
    return {
        "execution_id": rep.execution_id,
        "status": rep.status.value,
        "transaction_signature": rep.signature,
        "blockhash": rep.blockhash,
        "last_valid_block_height": rep.last_valid_block_height,
        "slot": rep.slot,
        "requested_amount": rep.requested_amount,
        "actual_in": rep.actual_in,
        "actual_out": rep.actual_out,
        "expected_price": rep.expected_price,
        "actual_price": rep.actual_price,
        "slippage": rep.slippage_vs_expected,
        "platform_fees": rep.platform_fees,
        "network_fees": rep.network_fees,
        "priority_fee": rep.priority_fee,
        "error": rep.error,
        "submitted_at": rep.submitted_at,
        "completed_at": rep.completed_at,
        "extra": rep.extra,
    }
