# Configuration & environment variables

Two sources, deliberately separated:

| | Where | Hashed into `configuration_version` | Logged |
|---|---|---|---|
| Behaviour (`AppConfig`) | `config/default.yaml` + `--config` overlays + `TA__SECTION__KEY` env overrides | yes | yes |
| Secrets (`Secrets`) | environment / `.env` only | never | never (redacted, scrubbed) |
| Live ceilings (`LiveLimits`) | `config/live_limits.yaml` | applied as `min(config, live)` in LIVE | yes |

Unknown keys are rejected (`extra="forbid"`), risk fractions are range-checked, score weights must sum to 1.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes (except `--no-db` experiments) | `postgresql+asyncpg://user:pass@host:5432/db` |
| `SOLANA_RPC_URLS` | SHADOW (simulation), LIVE, enrichment | comma-separated HTTP RPC URLs in priority order |
| `SOLANA_WS_URLS` | live ingestion (`--source ws`) | comma-separated WebSocket URLs |
| `API_ADMIN_TOKEN` | LIVE; any control POST | ≥32 random chars. Without it all POST endpoints return 403 |
| `API_READ_TOKEN` | when `api.require_auth_for_reads` (default true) | GET endpoints |
| `TA_API_READ_TOKEN`, `TA_API_URL` | dashboard server | server-side proxy credentials (never sent to the browser) |
| `DASHBOARD_BASIC_AUTH` | dashboard | `user:password`; dashboard refuses to serve without it (`DASHBOARD_ALLOW_NO_AUTH=1` for local dev only) |
| `ANTHROPIC_API_KEY` | if `ai.enabled` | AI analyst |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | if `alerts.telegram_enabled` | alerts |
| `WALLET_KEYPAIR_PATH` | LIVE (SHADOW optional) | dedicated wallet keypair JSON, mode 0600 |
| `WALLET_PRIVATE_KEY_B58` | alternative | base58 secret (less safe than a mounted file) |
| `LIVE_TRADING_CONFIRM` | LIVE | must equal `I_UNDERSTAND_REAL_FUNDS_ARE_AT_RISK` |
| `POSTGRES_PASSWORD` | docker-compose | database password |
| `TA_CONFIG` | optional | base config path (default `config/default.yaml`) |
| `TA__<SECTION>__<KEY>` | optional | override any config value, e.g. `TA__RISK__MAX_DAILY_LOSS=0.03` |

## Key behaviour settings (all in `config/default.yaml`)

| Section | Highlights |
|---|---|
| `mode` | `RESEARCH` / `PAPER` (default) / `SHADOW` / `LIVE` |
| `filters` | min liquidity, max entry impact, max exit slippage, holder/creator concentration, activity, staleness, volatility, age, bundled launch, sniper share, near-graduation, injection |
| `scoring` | group weights (sum 1.0) and per-feature linear maps |
| `regime` | thresholds and per-regime overrides (size multiplier, score add, EV multiplier, hold multiplier, entries on/off) |
| `strategy` | `min_score`, `min_expected_value_frac`, stops, TP ladder, trailing, time stop, thesis rules |
| `ev` | `min_samples` (INSUFFICIENT DATA below), buckets, regime conditioning, Wilson z, outcome source |
| `risk` | `max_risk_per_trade`, `max_position_pct`, `max_portfolio_exposure`, `max_daily_loss`, `max_drawdown`, `max_open_positions`, liquidity share, wallet floor |
| `execution` | slippage bps, max total cost, CU limit, priority fee policy, simulation_required, confirmation timeouts, paper fill model |
| `ai` | `enabled` (default false), `required_for_entry`, model, effort, timeout, veto thresholds |
| `killswitch` | failure counts/windows, slippage anomaly, stale data, RPC, DB error thresholds, emergency exit |
| `backtest`, `research` | evaluation tick, walk-forward windows, threshold grid, label horizons |

Every value is an initial default, not a tuned optimum. Change values through config files under version
control; the resulting `configuration_version` appears on every decision.
