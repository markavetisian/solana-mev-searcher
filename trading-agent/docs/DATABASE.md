# Database schema

PostgreSQL 16 (SQLite supported for local experiments/tests only). Schema is managed by Alembic:
`migrations/versions/0001_initial_schema.py` (tables) and `0002_append_only_triggers.py` (immutability).

Apply with `ta migrate` (uses `DATABASE_URL`). This file is generated from `src/tradingagent/storage/models.py`.

## Immutability

* **Append-only (UPDATE/DELETE/TRUNCATE raise in PostgreSQL):** `token_state_transitions`, `wallet_events`, `features`, `scores`, `ai_analyses`, `signals`, `orders`, `execution_events`, `position_events`, `trades`, `portfolio_snapshots`, `risk_events`, `system_events`, `outcome_samples`, `signal_outcomes`, `operator_commands_log`
* **Raw market data (no UPDATE; DELETE allowed for retention jobs):** `market_events`, `market_snapshots`
* **Mutable current-state tables, each with an append-only log:** `tokens` → `token_state_transitions`, `positions` → `position_events`, `executions` → `execution_events`, `wallets` → `wallet_events`, `operator_commands` → `operator_commands_log`, `runtime_state` (worker heartbeat, published state, latched kill switch).
* Paper resets never delete history: they start a new `session_id`.

## Versioning on every decision

`signals` (one row per decision, approved or not) carries `strategy_version`, `feature_version`, `configuration_version`, `model_version`, `ai_model` and the full `versions` JSON; `orders` and `trades` carry the same, so "which exact version of the strategy made this trade?" is one join. `configuration_versions` stores the full config for each hash; `strategy_versions` the component versions and the Pump IDL commit.

## Tables

### `ai_analyses`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `decision_id` | VARCHAR | no | index |
| `mint` | VARCHAR | no | index |
| `t` | FLOAT | no |  |
| `status` | VARCHAR | no |  |
| `model` | VARCHAR | yes |  |
| `prompt_version` | VARCHAR | no |  |
| `latency_ms` | FLOAT | no |  |
| `assessment` | JSON | yes |  |
| `error` | TEXT | yes |  |
| `input_hash` | VARCHAR | yes |  |

### `backtest_runs`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `created_at` | DATETIME | no |  |
| `kind` | VARCHAR | no |  |
| `params` | JSON | no |  |
| `results` | JSON | no |  |

### `configuration_versions`

| column | type | null | key |
|---|---|---|---|
| `version` | VARCHAR | no | PK |
| `created_at` | DATETIME | no |  |
| `config` | JSON | no |  |

### `execution_events`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `execution_id` | VARCHAR | no | index |
| `t` | FLOAT | no |  |
| `status` | VARCHAR | no |  |
| `detail` | JSON | no |  |

### `executions`

| column | type | null | key |
|---|---|---|---|
| `execution_id` | VARCHAR | no | PK |
| `status` | VARCHAR | no | index |
| `transaction_signature` | VARCHAR | yes | unique |
| `blockhash` | VARCHAR | yes |  |
| `last_valid_block_height` | BIGINT | yes |  |
| `slot` | BIGINT | yes |  |
| `requested_amount` | BIGINT | no |  |
| `actual_in` | BIGINT | no |  |
| `actual_out` | BIGINT | no |  |
| `expected_price` | FLOAT | no |  |
| `actual_price` | FLOAT | no |  |
| `slippage` | FLOAT | no |  |
| `platform_fees` | BIGINT | no |  |
| `network_fees` | BIGINT | no |  |
| `priority_fee` | BIGINT | no |  |
| `error` | TEXT | yes |  |
| `submitted_at` | FLOAT | yes |  |
| `completed_at` | FLOAT | yes |  |
| `extra` | JSON | no |  |
| `updated_at` | DATETIME | no |  |

Unique: (transaction_signature)

### `features`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `decision_id` | VARCHAR | no | index |
| `mint` | VARCHAR | no | index |
| `t` | FLOAT | no | index |
| `version` | VARCHAR | no |  |
| `values` | JSON | no |  |

### `market_events`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `kind` | VARCHAR | no |  |
| `mint` | VARCHAR | yes |  |
| `signature` | VARCHAR | no |  |
| `slot` | BIGINT | no | index |
| `seq` | INTEGER | no |  |
| `observed_at` | FLOAT | no |  |
| `chain_time` | FLOAT | no |  |
| `venue` | VARCHAR | yes |  |
| `side` | VARCHAR | yes |  |
| `user` | VARCHAR | yes |  |
| `sol_amount` | BIGINT | no |  |
| `token_amount` | BIGINT | no |  |
| `source` | VARCHAR | no |  |
| `payload` | JSON | no |  |

Unique: (signature, seq, kind)

Index `ix_market_events_mint_t`: (mint, observed_at)

### `market_snapshots`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `mint` | VARCHAR | no |  |
| `t` | FLOAT | no |  |
| `state` | VARCHAR | no |  |
| `venue` | VARCHAR | yes |  |
| `price_lamports_per_token` | FLOAT | yes |  |
| `market_cap_sol` | FLOAT | yes |  |
| `liquidity_sol` | FLOAT | yes |  |
| `volume_sol_5m` | FLOAT | yes |  |
| `buys_5m` | INTEGER | yes |  |
| `sells_5m` | INTEGER | yes |  |
| `holders` | INTEGER | yes |  |
| `progress` | FLOAT | yes |  |
| `payload` | JSON | no |  |

Index `ix_snap_mint_t`: (mint, t)

### `operator_commands`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `t` | FLOAT | no |  |
| `command` | VARCHAR | no |  |
| `args` | JSON | no |  |
| `operator` | VARCHAR | no |  |
| `status` | VARCHAR | no |  |
| `processed_at` | FLOAT | yes |  |
| `result` | JSON | yes |  |

### `operator_commands_log`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `t` | FLOAT | no |  |
| `command_id` | BIGINT | no |  |
| `command` | VARCHAR | no |  |
| `operator` | VARCHAR | no |  |
| `status` | VARCHAR | no |  |
| `detail` | JSON | no |  |

### `orders`

| column | type | null | key |
|---|---|---|---|
| `execution_id` | VARCHAR | no | PK |
| `decision_id` | VARCHAR | yes | index |
| `position_id` | VARCHAR | yes | index |
| `session_id` | VARCHAR | no | index |
| `mode` | VARCHAR | no |  |
| `mint` | VARCHAR | no | index |
| `side` | VARCHAR | no |  |
| `venue` | VARCHAR | no |  |
| `amount_in` | BIGINT | no |  |
| `min_out` | BIGINT | no |  |
| `expected_out` | BIGINT | no |  |
| `expected_price` | FLOAT | no |  |
| `spot_price` | FLOAT | no |  |
| `expected_slippage` | FLOAT | no |  |
| `priority_micro_lamports` | BIGINT | no |  |
| `reason` | TEXT | no |  |
| `created_at` | FLOAT | no |  |
| `versions` | JSON | no |  |

### `outcome_samples`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `t` | FLOAT | no | index |
| `score` | FLOAT | no |  |
| `regime` | VARCHAR | no |  |
| `gross_return` | FLOAT | no |  |
| `net_return` | FLOAT | no |  |
| `hold_s` | FLOAT | no |  |
| `source` | VARCHAR | no | index |
| `strategy_version` | VARCHAR | no | index |

### `portfolio_snapshots`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `session_id` | VARCHAR | no | index |
| `mode` | VARCHAR | no |  |
| `t` | FLOAT | no | index |
| `equity_sol` | FLOAT | no |  |
| `cash_sol` | FLOAT | no |  |
| `realized_pnl_sol` | FLOAT | no |  |
| `unrealized_pnl_sol` | FLOAT | no |  |
| `drawdown` | FLOAT | no |  |
| `open_positions` | INTEGER | no |  |
| `data` | JSON | no |  |

### `position_events`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `position_id` | VARCHAR | no | index |
| `t` | FLOAT | no |  |
| `event` | VARCHAR | no |  |
| `detail` | JSON | no |  |

### `positions`

| column | type | null | key |
|---|---|---|---|
| `position_id` | VARCHAR | no | PK |
| `session_id` | VARCHAR | no | index |
| `mode` | VARCHAR | no |  |
| `mint` | VARCHAR | no | index |
| `symbol` | TEXT | yes |  |
| `venue` | VARCHAR | no |  |
| `status` | VARCHAR | no | index |
| `opened_at` | FLOAT | no |  |
| `closed_at` | FLOAT | yes |  |
| `data` | JSON | no |  |
| `updated_at` | DATETIME | no |  |

### `research_experiments`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `name` | VARCHAR | no | index |
| `created_at` | DATETIME | no |  |
| `params` | JSON | no |  |
| `results` | JSON | no |  |
| `data_range` | JSON | no |  |
| `versions` | JSON | no |  |

### `risk_events`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `t` | FLOAT | no | index |
| `kind` | VARCHAR | no | index |
| `severity` | VARCHAR | no |  |
| `detail` | JSON | no |  |

### `runtime_state`

| column | type | null | key |
|---|---|---|---|
| `key` | VARCHAR | no | PK |
| `value` | JSON | no |  |
| `updated_at` | FLOAT | no |  |

### `scores`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `decision_id` | VARCHAR | no | index |
| `mint` | VARCHAR | no | index |
| `t` | FLOAT | no | index |
| `version` | VARCHAR | no |  |
| `total` | FLOAT | no |  |
| `components` | JSON | no |  |

### `signal_outcomes`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `decision_id` | VARCHAR | no | unique |
| `mint` | VARCHAR | no | index |
| `t` | FLOAT | no | index |
| `score` | FLOAT | yes |  |
| `regime` | VARCHAR | yes |  |
| `features` | JSON | no |  |
| `labels` | JSON | no |  |
| `feature_version` | VARCHAR | no |  |

Unique: (decision_id)

### `signals`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `decision_id` | VARCHAR | no | unique |
| `mint` | VARCHAR | no | index |
| `symbol` | TEXT | yes |  |
| `t` | FLOAT | no | index |
| `mode` | VARCHAR | no |  |
| `session_id` | VARCHAR | no | index |
| `outcome` | VARCHAR | no | index |
| `market_state` | VARCHAR | yes |  |
| `regime` | VARCHAR | yes |  |
| `score_total` | FLOAT | yes |  |
| `ev` | JSON | yes |  |
| `sizing` | JSON | yes |  |
| `execution_estimate` | JSON | yes |  |
| `gates` | JSON | no |  |
| `rejection_reasons` | JSON | no |  |
| `strategy_version` | VARCHAR | no |  |
| `feature_version` | VARCHAR | no |  |
| `configuration_version` | VARCHAR | no |  |
| `model_version` | VARCHAR | no |  |
| `ai_model` | VARCHAR | no |  |
| `versions` | JSON | no |  |

Unique: (decision_id)

### `strategy_versions`

| column | type | null | key |
|---|---|---|---|
| `version` | VARCHAR | no | PK |
| `created_at` | DATETIME | no |  |
| `components` | JSON | no |  |

### `system_events`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `t` | FLOAT | no | index |
| `component` | VARCHAR | no |  |
| `event` | VARCHAR | no |  |
| `severity` | VARCHAR | no |  |
| `detail` | JSON | no |  |

### `token_state_transitions`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `mint` | VARCHAR | no | index |
| `t` | FLOAT | no |  |
| `from_state` | VARCHAR | no |  |
| `to_state` | VARCHAR | no |  |
| `reason` | TEXT | no |  |

### `tokens`

| column | type | null | key |
|---|---|---|---|
| `mint` | VARCHAR | no | PK |
| `name` | TEXT | yes |  |
| `symbol` | TEXT | yes |  |
| `uri` | TEXT | yes |  |
| `creator` | VARCHAR | yes | index |
| `created_at` | FLOAT | yes |  |
| `market_state` | VARCHAR | no | index |
| `bonding_curve_address` | VARCHAR | yes |  |
| `pool_address` | VARCHAR | yes | index |
| `graduation_state` | VARCHAR | no |  |
| `first_seen_slot` | BIGINT | no |  |
| `last_seen_slot` | BIGINT | no |  |
| `first_seen_at` | FLOAT | no |  |
| `last_trade_at` | FLOAT | yes |  |
| `partial_history` | BOOLEAN | no |  |
| `token_program` | VARCHAR | yes |  |
| `quote_mint` | VARCHAR | yes |  |
| `is_mayhem` | BOOLEAN | no |  |
| `untradeable_reason` | TEXT | yes |  |
| `updated_at` | DATETIME | no |  |

### `trades`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `position_id` | VARCHAR | no | unique |
| `session_id` | VARCHAR | no | index |
| `mode` | VARCHAR | no |  |
| `mint` | VARCHAR | no | index |
| `opened_at` | FLOAT | no |  |
| `closed_at` | FLOAT | no | index |
| `pnl_sol` | FLOAT | no |  |
| `net_return` | FLOAT | no |  |
| `exit_reason` | VARCHAR | no |  |
| `strategy_version` | VARCHAR | no |  |
| `configuration_version` | VARCHAR | no |  |
| `data` | JSON | no |  |

Unique: (position_id)

### `wallet_events`

| column | type | null | key |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `wallet` | VARCHAR | no | index |
| `t` | FLOAT | no |  |
| `kind` | VARCHAR | no |  |
| `mint` | VARCHAR | yes |  |
| `payload` | JSON | no |  |

### `wallets`

| column | type | null | key |
|---|---|---|---|
| `address` | VARCHAR | no | PK |
| `stats` | JSON | no |  |
| `is_smart` | BOOLEAN | no |  |
| `cluster` | VARCHAR | yes |  |
| `updated_at` | DATETIME | no |  |

