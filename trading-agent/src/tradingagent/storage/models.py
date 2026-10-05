"""Database schema (SQLAlchemy 2.0). PostgreSQL in production; SQLite for tests.

Append-only tables (decision audit trail) are protected by triggers in PostgreSQL (see migrations): UPDATE and
DELETE raise. Mutable "current state" tables (tokens, positions, executions, wallets, runtime_state) always have an
append-only companion log (token_state_transitions, position_events, execution_events, wallet_events).
Paper resets never delete: they start a new session_id.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")  # sqlite autoincrement needs INTEGER PRIMARY KEY


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


APPEND_ONLY_TABLES = (
    "market_events",
    "market_snapshots",
    "token_state_transitions",
    "wallet_events",
    "features",
    "scores",
    "ai_analyses",
    "signals",
    "orders",
    "execution_events",
    "position_events",
    "trades",
    "portfolio_snapshots",
    "risk_events",
    "system_events",
    "outcome_samples",
    "signal_outcomes",
    "operator_commands_log",
)
# Market data may be deleted by retention jobs; decision records may never be deleted or updated.
RETENTION_DELETABLE = ("market_events", "market_snapshots")


class Token(Base):
    __tablename__ = "tokens"
    mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str | None] = mapped_column(Text)
    creator: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[float | None] = mapped_column(Float)
    market_state: Mapped[str] = mapped_column(String(32), index=True)
    bonding_curve_address: Mapped[str | None] = mapped_column(String(64))
    pool_address: Mapped[str | None] = mapped_column(String(64), index=True)
    graduation_state: Mapped[str] = mapped_column(String(16), default="NONE")
    first_seen_slot: Mapped[int] = mapped_column(BigInteger)
    last_seen_slot: Mapped[int] = mapped_column(BigInteger)
    first_seen_at: Mapped[float] = mapped_column(Float)
    last_trade_at: Mapped[float | None] = mapped_column(Float)
    partial_history: Mapped[bool] = mapped_column(Boolean, default=False)
    token_program: Mapped[str | None] = mapped_column(String(64))
    quote_mint: Mapped[str | None] = mapped_column(String(64))
    is_mayhem: Mapped[bool] = mapped_column(Boolean, default=False)
    untradeable_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TokenStateTransition(Base):
    __tablename__ = "token_state_transitions"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    t: Mapped[float] = mapped_column(Float)
    from_state: Mapped[str] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)


class MarketEventRow(Base):
    __tablename__ = "market_events"
    __table_args__ = (
        UniqueConstraint("signature", "seq", "kind", name="uq_market_event"),
        Index("ix_market_events_mint_t", "mint", "observed_at"),
        Index("ix_market_events_t", "observed_at"),
    )
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(24))
    mint: Mapped[str | None] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(100))
    slot: Mapped[int] = mapped_column(BigInteger, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[float] = mapped_column(Float)
    chain_time: Mapped[float] = mapped_column(Float)
    venue: Mapped[str | None] = mapped_column(String(16))
    side: Mapped[str | None] = mapped_column(String(4))
    user: Mapped[str | None] = mapped_column(String(64))
    sol_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    token_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    source: Mapped[str] = mapped_column(String(16), default="live")
    payload: Mapped[dict] = mapped_column(JsonType)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_snap_mint_t", "mint", "t"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(64))
    t: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(32))
    venue: Mapped[str | None] = mapped_column(String(16))
    price_lamports_per_token: Mapped[float | None] = mapped_column(Float)
    market_cap_sol: Mapped[float | None] = mapped_column(Float)
    liquidity_sol: Mapped[float | None] = mapped_column(Float)
    volume_sol_5m: Mapped[float | None] = mapped_column(Float)
    buys_5m: Mapped[int | None] = mapped_column(Integer)
    sells_5m: Mapped[int | None] = mapped_column(Integer)
    holders: Mapped[int | None] = mapped_column(Integer)
    progress: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JsonType)


class Wallet(Base):
    __tablename__ = "wallets"
    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    stats: Mapped[dict] = mapped_column(JsonType)
    is_smart: Mapped[bool] = mapped_column(Boolean, default=False)
    cluster: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WalletEvent(Base):
    __tablename__ = "wallet_events"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(64), index=True)
    t: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(32))
    mint: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JsonType)


class FeatureRow(Base):
    __tablename__ = "features"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(40), index=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    t: Mapped[float] = mapped_column(Float, index=True)
    version: Mapped[str] = mapped_column(String(32))
    values: Mapped[dict] = mapped_column(JsonType)


class ScoreRow(Base):
    __tablename__ = "scores"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(40), index=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    t: Mapped[float] = mapped_column(Float, index=True)
    version: Mapped[str] = mapped_column(String(32))
    total: Mapped[float] = mapped_column(Float)
    components: Mapped[dict] = mapped_column(JsonType)


class AIAnalysisRow(Base):
    __tablename__ = "ai_analyses"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(40), index=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    t: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(40))
    latency_ms: Mapped[float] = mapped_column(Float)
    assessment: Mapped[dict | None] = mapped_column(JsonType)
    error: Mapped[str | None] = mapped_column(Text)
    input_hash: Mapped[str | None] = mapped_column(String(32))


class Signal(Base):
    """Every decision (approved, rejected or no-trade) with all gate results and versions."""

    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(40), unique=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(Text)
    t: Mapped[float] = mapped_column(Float, index=True)
    mode: Mapped[str] = mapped_column(String(12))
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    outcome: Mapped[str] = mapped_column(String(12), index=True)
    market_state: Mapped[str | None] = mapped_column(String(32))
    regime: Mapped[str | None] = mapped_column(String(24))
    score_total: Mapped[float | None] = mapped_column(Float)
    ev: Mapped[dict | None] = mapped_column(JsonType)
    sizing: Mapped[dict | None] = mapped_column(JsonType)
    execution_estimate: Mapped[dict | None] = mapped_column(JsonType)
    gates: Mapped[list] = mapped_column(JsonType)
    rejection_reasons: Mapped[list] = mapped_column(JsonType)
    strategy_version: Mapped[str] = mapped_column(String(40))
    feature_version: Mapped[str] = mapped_column(String(40))
    configuration_version: Mapped[str] = mapped_column(String(40))
    model_version: Mapped[str] = mapped_column(String(40))
    ai_model: Mapped[str] = mapped_column(String(64))
    versions: Mapped[dict] = mapped_column(JsonType)


class Order(Base):
    __tablename__ = "orders"
    execution_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    decision_id: Mapped[str | None] = mapped_column(String(40), index=True)
    position_id: Mapped[str | None] = mapped_column(String(40), index=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    mode: Mapped[str] = mapped_column(String(12))
    mint: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))
    venue: Mapped[str] = mapped_column(String(16))
    amount_in: Mapped[int] = mapped_column(BigInteger)
    min_out: Mapped[int] = mapped_column(BigInteger)
    expected_out: Mapped[int] = mapped_column(BigInteger)
    expected_price: Mapped[float] = mapped_column(Float)
    spot_price: Mapped[float] = mapped_column(Float)
    expected_slippage: Mapped[float] = mapped_column(Float)
    priority_micro_lamports: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
    versions: Mapped[dict] = mapped_column(JsonType)


class Execution(Base):
    __tablename__ = "executions"
    execution_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    transaction_signature: Mapped[str | None] = mapped_column(String(100), unique=True)
    blockhash: Mapped[str | None] = mapped_column(String(64))
    last_valid_block_height: Mapped[int | None] = mapped_column(BigInteger)
    slot: Mapped[int | None] = mapped_column(BigInteger)
    requested_amount: Mapped[int] = mapped_column(BigInteger)
    actual_in: Mapped[int] = mapped_column(BigInteger, default=0)
    actual_out: Mapped[int] = mapped_column(BigInteger, default=0)
    expected_price: Mapped[float] = mapped_column(Float)
    actual_price: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    platform_fees: Mapped[int] = mapped_column(BigInteger, default=0)
    network_fees: Mapped[int] = mapped_column(BigInteger, default=0)
    priority_fee: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[float | None] = mapped_column(Float)
    completed_at: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(40), index=True)
    t: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JsonType)


class PositionRow(Base):
    __tablename__ = "positions"
    position_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    mode: Mapped[str] = mapped_column(String(12))
    mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(Text)
    venue: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), index=True)
    opened_at: Mapped[float] = mapped_column(Float)
    closed_at: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PositionEvent(Base):
    __tablename__ = "position_events"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    position_id: Mapped[str] = mapped_column(String(40), index=True)
    t: Mapped[float] = mapped_column(Float)
    event: Mapped[str] = mapped_column(String(24))
    detail: Mapped[dict] = mapped_column(JsonType)


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    position_id: Mapped[str] = mapped_column(String(40), unique=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    mode: Mapped[str] = mapped_column(String(12))
    mint: Mapped[str] = mapped_column(String(64), index=True)
    opened_at: Mapped[float] = mapped_column(Float)
    closed_at: Mapped[float] = mapped_column(Float, index=True)
    pnl_sol: Mapped[float] = mapped_column(Float)
    net_return: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(32))
    strategy_version: Mapped[str] = mapped_column(String(40))
    configuration_version: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JsonType)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    mode: Mapped[str] = mapped_column(String(12))
    t: Mapped[float] = mapped_column(Float, index=True)
    equity_sol: Mapped[float] = mapped_column(Float)
    cash_sol: Mapped[float] = mapped_column(Float)
    realized_pnl_sol: Mapped[float] = mapped_column(Float)
    unrealized_pnl_sol: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JsonType)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    t: Mapped[float] = mapped_column(Float, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(12))
    detail: Mapped[dict] = mapped_column(JsonType)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    t: Mapped[float] = mapped_column(Float, index=True)
    component: Mapped[str] = mapped_column(String(40))
    event: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(12))
    detail: Mapped[dict] = mapped_column(JsonType)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    version: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    components: Mapped[dict] = mapped_column(JsonType)


class ConfigurationVersion(Base):
    __tablename__ = "configuration_versions"
    version: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    config: Mapped[dict] = mapped_column(JsonType)


class OutcomeSampleRow(Base):
    __tablename__ = "outcome_samples"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    t: Mapped[float] = mapped_column(Float, index=True)
    score: Mapped[float] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(24))
    gross_return: Mapped[float] = mapped_column(Float)
    net_return: Mapped[float] = mapped_column(Float)
    hold_s: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), index=True)
    strategy_version: Mapped[str] = mapped_column(String(40), index=True)


class SignalOutcome(Base):
    """Research labels: what actually happened after each candidate signal."""

    __tablename__ = "signal_outcomes"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(40), unique=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    t: Mapped[float] = mapped_column(Float, index=True)
    score: Mapped[float | None] = mapped_column(Float)
    regime: Mapped[str | None] = mapped_column(String(24))
    features: Mapped[dict] = mapped_column(JsonType)
    labels: Mapped[dict] = mapped_column(JsonType)
    feature_version: Mapped[str] = mapped_column(String(40))


class ResearchExperiment(Base):
    __tablename__ = "research_experiments"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    params: Mapped[dict] = mapped_column(JsonType)
    results: Mapped[dict] = mapped_column(JsonType)
    data_range: Mapped[dict] = mapped_column(JsonType)
    versions: Mapped[dict] = mapped_column(JsonType)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(24))
    params: Mapped[dict] = mapped_column(JsonType)
    results: Mapped[dict] = mapped_column(JsonType)


class RuntimeState(Base):
    __tablename__ = "runtime_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JsonType)
    updated_at: Mapped[float] = mapped_column(Float)


class OperatorCommand(Base):
    __tablename__ = "operator_commands"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    t: Mapped[float] = mapped_column(Float)
    command: Mapped[str] = mapped_column(String(32))
    args: Mapped[dict] = mapped_column(JsonType)
    operator: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    processed_at: Mapped[float | None] = mapped_column(Float)
    result: Mapped[dict | None] = mapped_column(JsonType)


class OperatorCommandLog(Base):
    __tablename__ = "operator_commands_log"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    t: Mapped[float] = mapped_column(Float)
    command_id: Mapped[int] = mapped_column(BigInteger)
    command: Mapped[str] = mapped_column(String(32))
    operator: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JsonType)
