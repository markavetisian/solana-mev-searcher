"""Configuration.

Two separate sources:
  * AppConfig  — non-secret behaviour, loaded from YAML (config/default.yaml + optional override file +
                 TA__SECTION__KEY environment overrides). Its canonical hash is the `configuration_version`
                 stamped on every decision.
  * Secrets    — credentials, loaded ONLY from environment variables (or a .env file in development).
                 Never serialised, never hashed into the config version, never logged.

Live trading has an independent limits file (config/live_limits.yaml) that the worker loads read-only. Nothing
in the AI layer has a code path that writes configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tradingagent.common.types import Mode, Regime

_STRICT = ConfigDict(extra="forbid", frozen=True)


class SolanaConfig(BaseModel):
    model_config = _STRICT
    commitment: Literal["processed", "confirmed", "finalized"] = "confirmed"
    rpc_timeout_s: float = 8.0
    rpc_max_retries_per_call: int = 2
    rpc_circuit_open_after_errors: int = 5
    rpc_circuit_cooldown_s: float = 20.0
    ws_ping_interval_s: float = 10.0
    ws_stale_after_s: float = 20.0  # no message for this long => reconnect
    ws_reconnect_max_backoff_s: float = 15.0
    subscribe_pumpswap: bool = True


class PumpConfig(BaseModel):
    model_config = _STRICT
    # Fallback fee schedule used when the on-chain FeeConfig cannot be fetched (e.g. backtests without RPC).
    # Rows: [market_cap_threshold_lamports, lp_bps, protocol_bps, creator_bps] — mainnet values 2026-09-08.
    fee_tiers: list[tuple[int, int, int, int]] = Field(
        default_factory=lambda: [
            (0, 2, 93, 30),
            (420_000_000_000, 20, 5, 95),
            (1_470_000_000_000, 20, 5, 90),
            (2_460_000_000_000, 20, 5, 85),
            (3_440_000_000_000, 20, 5, 80),
            (4_420_000_000_000, 20, 5, 75),
            (9_820_000_000_000, 20, 5, 70),
            (14_740_000_000_000, 20, 5, 65),
            (19_650_000_000_000, 20, 5, 60),
            (24_560_000_000_000, 20, 5, 55),
            (29_470_000_000_000, 20, 5, 50),
            (34_380_000_000_000, 20, 5, 45),
            (39_300_000_000_000, 20, 5, 40),
            (44_210_000_000_000, 20, 5, 35),
            (49_120_000_000_000, 20, 5, 30),
            (54_030_000_000_000, 20, 5, 28),
            (58_940_000_000_000, 20, 5, 25),
            (63_860_000_000_000, 20, 5, 23),
            (68_770_000_000_000, 20, 5, 20),
            (73_681_000_000_000, 20, 5, 18),
            (78_590_000_000_000, 20, 5, 15),
            (83_500_000_000_000, 20, 5, 13),
            (88_400_000_000_000, 20, 5, 10),
            (93_330_000_000_000, 20, 5, 8),
            (98_240_000_000_000, 20, 5, 5),
        ]
    )
    flat_fees: tuple[int, int, int] = (25, 5, 0)  # non-canonical pools
    refresh_onchain_fee_config_s: float = 600.0
    initial_virtual_token_reserves: int = 1_073_000_000_000_000
    initial_virtual_sol_reserves: int = 30_000_000_000
    initial_real_token_reserves: int = 793_100_000_000_000
    token_total_supply: int = 1_000_000_000_000_000
    allow_mayhem_mode: bool = False
    allow_non_sol_quote: bool = False


class TrackingConfig(BaseModel):
    model_config = _STRICT
    max_tracked_tokens: int = 5_000
    evict_after_idle_s: float = 3_600.0
    dead_after_idle_s: float = 900.0  # no trades for this long => DEAD
    dead_below_liquidity_sol: float = 0.5
    active_after_pool_trades: int = 20
    series_retention_s: float = 4 * 3_600.0
    snapshot_interval_s: float = 5.0
    holder_enrichment_enabled: bool = False  # getTokenLargestAccounts calls (RPC cost)
    holder_enrichment_min_interval_s: float = 30.0
    funding_enrichment_enabled: bool = False


class FilterConfig(BaseModel):
    """Hard rejection rules evaluated before any scoring. Every rejection is logged with observed vs limit."""

    model_config = _STRICT
    min_liquidity_sol: float = 5.0
    max_entry_price_impact: float = 0.04
    max_expected_exit_slippage: float = 0.07
    max_top10_concentration: float = 0.45  # excluding the bonding curve / pool account
    max_creator_holding: float = 0.08
    reject_if_creator_sold_pct_over: float = 0.5
    min_trades_60s: int = 8
    min_unique_traders_300s: int = 15
    min_volume_sol_300s: float = 3.0
    max_data_staleness_s: float = 4.0
    max_volatility_60s: float = 0.25  # stdev of 5s log returns
    min_token_age_s: float = 20.0
    max_token_age_s: float = 6 * 3_600.0
    reject_partial_history: bool = True
    max_same_slot_launch_buyers: int = 6  # bundled-launch heuristic
    max_sniper_supply_share: float = 0.35
    reject_metadata_injection: bool = True
    allowed_states: list[str] = Field(default_factory=lambda: ["BONDING_CURVE", "PUMPSWAP", "ACTIVE"])
    max_bonding_progress: float = 0.95  # too close to graduation: liquidity about to freeze
    creator_suspicion_reject_confidence: float = 0.8


class ScoreComponentMap(BaseModel):
    """Maps a raw feature to 0..100 linearly between `lo` and `hi` (inverted when lo > hi)."""

    model_config = _STRICT
    feature: str
    lo: float
    hi: float
    weight: float = 1.0


class ScoringConfig(BaseModel):
    model_config = _STRICT
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "momentum": 0.20,
            "volume": 0.15,
            "flow": 0.15,
            "holders": 0.10,
            "liquidity": 0.10,
            "wallets": 0.10,
            "creator": 0.05,
            "volatility": 0.05,
            "structure": 0.05,
            "exit_quality": 0.05,
        }
    )
    components: dict[str, list[ScoreComponentMap]] = Field(
        default_factory=lambda: {
            "momentum": [
                ScoreComponentMap(feature="ret_60s", lo=-0.05, hi=0.25, weight=0.5),
                ScoreComponentMap(feature="ret_300s", lo=-0.10, hi=0.60, weight=0.3),
                ScoreComponentMap(feature="persistence_60s", lo=0.3, hi=0.8, weight=0.2),
            ],
            "volume": [
                ScoreComponentMap(feature="vol_accel_30s", lo=0.8, hi=3.0, weight=0.5),
                ScoreComponentMap(feature="buy_vol_accel_30s", lo=0.8, hi=3.0, weight=0.3),
                ScoreComponentMap(feature="vol_liq_ratio_300s", lo=0.2, hi=3.0, weight=0.2),
            ],
            "flow": [
                ScoreComponentMap(feature="imbalance_60s", lo=-0.1, hi=0.5, weight=0.5),
                ScoreComponentMap(feature="buyer_seller_ratio_60s", lo=0.8, hi=3.0, weight=0.3),
                ScoreComponentMap(feature="sell_burst_15s", lo=3.0, hi=0.0, weight=0.2),
            ],
            "holders": [
                ScoreComponentMap(feature="holder_growth_60s", lo=0.0, hi=0.3, weight=0.4),
                ScoreComponentMap(feature="top10_share", lo=0.45, hi=0.15, weight=0.4),
                ScoreComponentMap(feature="holder_retention", lo=0.3, hi=0.9, weight=0.2),
            ],
            "liquidity": [
                ScoreComponentMap(feature="liquidity_sol", lo=5.0, hi=60.0, weight=0.5),
                ScoreComponentMap(feature="liq_change_60s", lo=-0.1, hi=0.3, weight=0.5),
            ],
            "wallets": [
                ScoreComponentMap(feature="smart_wallet_buy_share_300s", lo=0.0, hi=0.3, weight=0.6),
                ScoreComponentMap(feature="whale_net_flow_300s", lo=-0.05, hi=0.05, weight=0.4),
            ],
            "creator": [
                ScoreComponentMap(feature="creator_holding", lo=0.08, hi=0.0, weight=0.5),
                ScoreComponentMap(feature="creator_prev_graduation_rate", lo=0.0, hi=0.2, weight=0.5),
            ],
            "volatility": [ScoreComponentMap(feature="volatility_60s", lo=0.20, hi=0.02, weight=1.0)],
            "structure": [ScoreComponentMap(feature="hh_hl_score_60s", lo=0.0, hi=1.0, weight=1.0)],
            "exit_quality": [ScoreComponentMap(feature="exit_impact_frac", lo=0.07, hi=0.005, weight=1.0)],
        }
    )
    missing_feature_score: float = 0.0  # unknown is not neutral: missing data scores zero

    @model_validator(mode="after")
    def _weights_sum(self) -> ScoringConfig:
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"scoring.weights must sum to 1.0 (got {total})")
        missing = set(self.weights) - set(self.components)
        if missing:
            raise ValueError(f"scoring.components missing groups: {sorted(missing)}")
        return self


class RegimeOverride(BaseModel):
    model_config = _STRICT
    entries_enabled: bool = True
    size_multiplier: float = 1.0
    min_score_add: float = 0.0
    min_ev_multiplier: float = 1.0
    max_hold_multiplier: float = 1.0


class RegimeConfig(BaseModel):
    model_config = _STRICT
    window_s: float = 300.0
    low_activity_trades_per_min: float = 200.0
    high_momentum_median_ret: float = 0.05
    risk_off_median_ret: float = -0.05
    extreme_volatility_dispersion: float = 1.0
    risk_on_up_fraction: float = 0.6
    degraded_staleness_s: float = 10.0
    overrides: dict[str, RegimeOverride] = Field(
        default_factory=lambda: {
            Regime.DEGRADED.value: RegimeOverride(entries_enabled=False),
            Regime.LOW_ACTIVITY.value: RegimeOverride(size_multiplier=0.5, min_score_add=5),
            Regime.NORMAL.value: RegimeOverride(),
            Regime.HIGH_MOMENTUM.value: RegimeOverride(),
            Regime.RISK_ON.value: RegimeOverride(),
            Regime.RISK_OFF.value: RegimeOverride(size_multiplier=0.5, min_score_add=10, min_ev_multiplier=1.5),
            Regime.EXTREME_VOLATILITY.value: RegimeOverride(
                size_multiplier=0.25, min_score_add=15, min_ev_multiplier=2.0, max_hold_multiplier=0.5
            ),
        }
    )


class StrategyConfig(BaseModel):
    model_config = _STRICT
    min_score: float = 70.0
    min_expected_value_frac: float = 0.02  # conservative EV / position size, after all costs
    max_hold_s: float = 900.0
    evaluation_cooldown_s: float = 3.0  # per token
    reentry_cooldown_s: float = 600.0
    hard_stop_frac: float = 0.15  # on executable (sell-quote) value
    take_profit_levels: list[tuple[float, float]] = Field(
        default_factory=lambda: [(0.20, 0.33), (0.40, 0.33), (0.80, 1.0)]
    )  # (gain, fraction of remaining position to sell)
    trailing_stop_frac: float = 0.12
    trailing_activation_gain: float = 0.10
    time_stop_s: float = 300.0  # exit if thesis has not developed
    time_stop_min_gain: float = 0.05
    thesis_imbalance_floor: float = -0.25
    thesis_liquidity_drop_frac: float = 0.30
    thesis_exit_on_creator_sell: bool = True
    exit_on_graduation: bool = False  # otherwise hold through migration and exit on PumpSwap
    exit_check_interval_s: float = 1.0


class EVConfig(BaseModel):
    model_config = _STRICT
    min_samples: int = 50  # per score bucket; fewer => INSUFFICIENT DATA => no trade
    score_buckets: list[float] = Field(default_factory=lambda: [0, 50, 60, 70, 80, 90, 101])
    condition_on_regime: bool = True
    min_samples_regime: int = 30
    confidence_z: float = 1.645  # one-sided 95% Wilson lower bound on P(win)
    execution_risk_per_vol: float = 0.5  # adverse-move cost per unit (volatility * sqrt(latency/5s))
    outcome_lookback_days: float = 30.0
    use_strategy_version_prefix: str = "pump-momentum-0."
    outcome_source: Literal["paper", "shadow", "backtest", "any"] = "any"


class RiskConfig(BaseModel):
    model_config = _STRICT
    max_risk_per_trade: float = 0.01  # fraction of equity lost if the hard stop fills as estimated
    max_position_pct: float = 0.05
    max_portfolio_exposure: float = 0.25
    max_daily_loss: float = 0.05
    max_drawdown: float = 0.20
    max_open_positions: int = 5
    max_liquidity_share: float = 0.02  # position cost vs pool quote liquidity
    min_position_sol: float = 0.02
    min_wallet_balance_sol: float = 0.10  # never trade the wallet below this
    reserve_sol_for_fees: float = 0.02
    max_consecutive_losses_before_pause: int = 8
    volatility_target_60s: float = 0.06  # size scales down when 60s volatility exceeds this

    @field_validator("max_risk_per_trade", "max_position_pct", "max_portfolio_exposure", "max_daily_loss")
    @classmethod
    def _sane_fraction(cls, v: float) -> float:
        if not 0 < v <= 0.5:
            raise ValueError("risk fractions must be in (0, 0.5]")
        return v


class PriorityFeePolicy(BaseModel):
    model_config = _STRICT
    mode: Literal["static", "percentile"] = "percentile"
    static_micro_lamports: int = 200_000
    percentile: float = 75.0
    min_micro_lamports: int = 10_000
    max_micro_lamports: int = 2_000_000
    max_priority_fee_lamports: int = 2_000_000  # hard cap per transaction


class ExecutionConfig(BaseModel):
    model_config = _STRICT
    max_slippage_bps: int = 500  # min_out protection on-chain
    max_total_cost_frac: float = 0.06  # entry+exit fees, impact, network, as fraction of size
    compute_unit_limit: int = 150_000
    compute_unit_buffer: float = 1.15
    priority_fee: PriorityFeePolicy = Field(default_factory=PriorityFeePolicy)
    simulation_required: bool = True
    confirm_timeout_s: float = 45.0
    status_poll_interval_s: float = 0.8
    max_send_attempts: int = 3  # rebroadcast of the SAME signed tx only while its blockhash is valid
    assumed_latency_s: float = 1.2  # decision -> landed, used by paper/backtest fill model
    paper_failure_rate: float = 0.03
    paper_extra_adverse_bps: float = 25.0
    max_price_feed_divergence: float = 0.03  # event-derived vs RPC-account price


class PaperConfig(BaseModel):
    model_config = _STRICT
    starting_equity_sol: float = 10.0


class AIConfig(BaseModel):
    model_config = _STRICT
    enabled: bool = False
    required_for_entry: bool = False
    provider: Literal["anthropic", "none"] = "anthropic"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high"] = "low"
    timeout_s: float = 12.0
    max_tokens: int = 2_000
    server_side_fallback: bool = True
    max_calls_per_minute: int = 20
    cache_ttl_s: float = 60.0
    # The AI can only ever make the system MORE conservative: it may veto, never approve or upsize.
    veto_on_negative: bool = True
    veto_min_confidence: float = 0.7
    max_metadata_chars: int = 200


class AlertsConfig(BaseModel):
    model_config = _STRICT
    telegram_enabled: bool = False
    min_score_alert: float = 85.0
    max_messages_per_minute: int = 20


class KillSwitchConfig(BaseModel):
    model_config = _STRICT
    max_execution_failures: int = 3
    execution_failure_window_s: float = 600.0
    max_abnormal_slippage_events: int = 3
    abnormal_slippage_multiple: float = 2.0  # actual slippage > multiple * expected
    stale_data_kill_after_s: float = 60.0
    rpc_degraded_kill_after_s: float = 120.0
    db_error_kill_after: int = 5
    balance_mismatch_tolerance_sol: float = 0.01
    emergency_exit_on_kill: bool = False


class StorageConfig(BaseModel):
    model_config = _STRICT
    batch_size: int = 500
    flush_interval_s: float = 1.0
    max_queue: int = 200_000
    persist_snapshots: bool = True
    persist_features: bool = True
    jsonl_archive_dir: str | None = None


class ApiConfig(BaseModel):
    model_config = _STRICT
    host: str = "127.0.0.1"
    port: int = 8080
    require_auth_for_reads: bool = True
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    rate_limit_per_minute: int = 240
    worker_heartbeat_stale_s: float = 10.0


class BacktestConfig(BaseModel):
    model_config = _STRICT
    evaluation_interval_s: float = 1.0
    train_days: float = 7.0
    test_days: float = 2.0
    step_days: float = 2.0
    min_test_trades: int = 30
    bootstrap_samples: int = 2_000
    allow_synthetic: bool = False
    score_threshold_grid: list[float] = Field(default_factory=lambda: [60.0, 70.0, 80.0])


class ResearchConfig(BaseModel):
    model_config = _STRICT
    label_horizons_s: list[int] = Field(default_factory=lambda: [30, 60, 300, 900])
    label_notional_sol: float = 0.5
    min_samples: int = 200
    candidate_min_trades_60s: int = 3


class AppConfig(BaseModel):
    model_config = _STRICT
    mode: Mode = Mode.PAPER
    log_level: str = "INFO"
    solana: SolanaConfig = Field(default_factory=SolanaConfig)
    pump: PumpConfig = Field(default_factory=PumpConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    ev: EVConfig = Field(default_factory=EVConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    killswitch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def configuration_version(self) -> str:
        return "cfg-" + hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]

    def with_overrides(self, **sections: Any) -> AppConfig:
        """Return a copy with deep-merged section overrides (used by backtests/walk-forward)."""
        data = self.model_dump(mode="python")
        _deep_merge(data, sections)
        return AppConfig.model_validate(data)


class LiveLimits(BaseModel):
    """Independent, operator-owned ceilings for LIVE mode. Applied as min(config, live_limits)."""

    model_config = _STRICT
    max_position_sol: float = 0.1
    max_total_exposure_sol: float = 0.3
    max_daily_loss_sol: float = 0.15
    max_open_positions: int = 2
    max_slippage_bps: int = 500
    max_priority_fee_lamports: int = 1_000_000
    min_wallet_balance_sol: float = 0.1
    max_trades_per_hour: int = 6
    allowed_venues: list[str] = Field(default_factory=lambda: ["BONDING_CURVE", "PUMPSWAP"])


class Secrets(BaseSettings):
    """Credentials. Environment only. Each value is SecretStr so repr/str/logging never reveals it."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./trading_agent.db")
    redis_url: SecretStr | None = None
    solana_rpc_urls: SecretStr = SecretStr("")  # comma-separated, in priority order
    solana_ws_urls: SecretStr = SecretStr("")  # comma-separated
    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    api_admin_token: SecretStr | None = None  # required for POST endpoints
    api_read_token: SecretStr | None = None
    # Wallet: path to a 0600 JSON keypair file (preferred) OR a base58 secret. Only LiveExecutionProvider and
    # ShadowExecutionProvider(simulate=True) ever read these.
    wallet_keypair_path: str | None = None
    wallet_private_key_b58: SecretStr | None = None
    live_trading_confirm: str | None = None  # must equal LIVE_CONFIRM_PHRASE for LIVE mode

    def rpc_urls(self) -> list[str]:
        return [u.strip() for u in self.solana_rpc_urls.get_secret_value().split(",") if u.strip()]

    def ws_urls(self) -> list[str]:
        return [u.strip() for u in self.solana_ws_urls.get_secret_value().split(",") if u.strip()]


LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_FUNDS_ARE_AT_RISK"


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _env_overrides(prefix: str = "TA__") -> dict:
    """TA__RISK__MAX_DAILY_LOSS=0.03 -> {"risk": {"max_daily_loss": "0.03"}} (pydantic coerces types)."""
    out: dict = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = [p.lower() for p in key[len(prefix) :].split("__") if p]
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        try:
            cur[parts[-1]] = json.loads(value)
        except json.JSONDecodeError:
            cur[parts[-1]] = value
    return out


def load_config(*paths: str | Path, env: bool = True) -> AppConfig:
    data: dict = {}
    for p in paths:
        if p and Path(p).exists():
            loaded = yaml.safe_load(Path(p).read_text()) or {}
            _deep_merge(data, loaded)
    if env:
        _deep_merge(data, _env_overrides())
    return AppConfig.model_validate(data)


def load_live_limits(path: str | Path = "config/live_limits.yaml") -> LiveLimits:
    p = Path(path)
    if not p.exists():
        return LiveLimits()
    return LiveLimits.model_validate(yaml.safe_load(p.read_text()) or {})


class LiveModeNotAuthorized(RuntimeError):
    pass


def live_guard(cfg: AppConfig, secrets: Secrets) -> None:
    """LIVE requires BOTH mode: LIVE in config AND the confirmation phrase in the environment AND a wallet."""
    if cfg.mode is not Mode.LIVE:
        return
    if secrets.live_trading_confirm != LIVE_CONFIRM_PHRASE:
        raise LiveModeNotAuthorized(f"mode=LIVE requires LIVE_TRADING_CONFIRM={LIVE_CONFIRM_PHRASE}")
    if not (secrets.wallet_keypair_path or secrets.wallet_private_key_b58):
        raise LiveModeNotAuthorized("mode=LIVE requires a dedicated trading wallet (WALLET_KEYPAIR_PATH)")
    if not secrets.rpc_urls():
        raise LiveModeNotAuthorized("mode=LIVE requires SOLANA_RPC_URLS")
    if not cfg.execution.simulation_required:
        raise LiveModeNotAuthorized("mode=LIVE requires execution.simulation_required=true")
    if not secrets.api_admin_token:
        raise LiveModeNotAuthorized("mode=LIVE requires API_ADMIN_TOKEN (control endpoints must be protected)")
