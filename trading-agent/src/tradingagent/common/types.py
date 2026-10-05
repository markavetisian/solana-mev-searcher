"""Shared enums and small value types."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Operating mode. LIVE is never the default and needs an explicit opt-in (see config.live_guard)."""

    RESEARCH = "RESEARCH"  # ingest + label + experiments, no order flow at all
    PAPER = "PAPER"  # live data, hypothetical fills
    SHADOW = "SHADOW"  # live data, real execution simulation (RPC simulateTransaction), never broadcast
    LIVE = "LIVE"  # real transactions


class LifecycleState(StrEnum):
    NEW = "NEW"
    BONDING_CURVE = "BONDING_CURVE"
    GRADUATING = "GRADUATING"  # curve complete, PumpSwap pool not yet observed: NOT tradeable
    PUMPSWAP = "PUMPSWAP"  # canonical pool created, too little post-migration history
    ACTIVE = "ACTIVE"  # trading on PumpSwap with enough history
    DEAD = "DEAD"
    UNTRADEABLE = "UNTRADEABLE"
    EXITED = "EXITED"  # tracking ended (evicted from memory)


TRADEABLE_STATES = frozenset({LifecycleState.BONDING_CURVE, LifecycleState.PUMPSWAP, LifecycleState.ACTIVE})


class Venue(StrEnum):
    BONDING_CURVE = "BONDING_CURVE"
    PUMPSWAP = "PUMPSWAP"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class EventKind(StrEnum):
    CREATE = "CREATE"
    TRADE = "TRADE"  # bonding-curve trade
    COMPLETE = "COMPLETE"  # bonding curve finished
    MIGRATE = "MIGRATE"  # liquidity migrated into PumpSwap
    POOL_CREATE = "POOL_CREATE"
    AMM_TRADE = "AMM_TRADE"
    HOLDER_SNAPSHOT = "HOLDER_SNAPSHOT"  # RPC enrichment, recorded so replays see exactly what live saw
    WALLET_FUNDING = "WALLET_FUNDING"  # RPC enrichment
    DATA_GAP = "DATA_GAP"  # stream reconnect / log truncation: data between slots may be missing


class Regime(StrEnum):
    DEGRADED = "DEGRADED"
    LOW_ACTIVITY = "LOW_ACTIVITY"
    NORMAL = "NORMAL"
    HIGH_MOMENTUM = "HIGH_MOMENTUM"
    EXTREME_VOLATILITY = "EXTREME_VOLATILITY"
    RISK_OFF = "RISK_OFF"
    RISK_ON = "RISK_ON"


class DecisionOutcome(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NO_TRADE = "NO_TRADE"  # nothing wrong, just no edge


class ExecStatus(StrEnum):
    CREATED = "CREATED"
    SIMULATED = "SIMULATED"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"  # landed with an error, or rejected before broadcast
    EXPIRED = "EXPIRED"  # blockhash expired without landing: provably not executed
    UNCERTAIN = "UNCERTAIN"  # cannot prove landed or not landed: NEVER auto-resend
    REJECTED = "REJECTED"  # rejected by pre-trade checks, never built
    FILLED_PAPER = "FILLED_PAPER"
    FILLED_SHADOW = "FILLED_SHADOW"


class ExitReason(StrEnum):
    HARD_STOP = "HARD_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_STOP = "TIME_STOP"
    THESIS_INVALIDATION = "THESIS_INVALIDATION"
    LIFECYCLE = "LIFECYCLE"
    REGIME = "REGIME"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"
    DEAD_TOKEN = "DEAD_TOKEN"
    END_OF_DATA = "END_OF_DATA"


class Severity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
