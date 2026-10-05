# Live execution

LIVE is **OFF by default** and cannot be enabled by a single setting.

## What must be true for the worker to start in LIVE

`common/config.py: live_guard` refuses to start unless ALL hold:

1. `mode: LIVE` in the effective config (e.g. `--config config/live.yaml`)
2. `LIVE_TRADING_CONFIRM=I_UNDERSTAND_REAL_FUNDS_ARE_AT_RISK` in the environment
3. A dedicated wallet: `WALLET_KEYPAIR_PATH` (file mode 0600, enforced) or `WALLET_PRIVATE_KEY_B58`
4. `SOLANA_RPC_URLS` configured
5. `execution.simulation_required: true`
6. `API_ADMIN_TOKEN` set (control endpoints must be protected)
7. A database (orders are persisted before execution; LIVE without a DB is refused)

`config/live_limits.yaml` is then loaded read-only and applied as an independent ceiling
(`min(config, live_limits)`) by the sizer, the risk engine **and** again inside `LiveExecutionProvider`
(max position SOL, total exposure, daily loss, open positions, slippage bps, priority fee, trades/hour, venues).
Nothing in the strategy or AI layer has a code path that writes configuration, keys, limits or permissions.

## Transaction path

`LiveExecutionProvider` → `build_instructions` (IDL-driven, see `pump/instructions.py`) → `TransactionEngine`:

1. Idempotency: one `execution_id` = one signed transaction. A repeated id returns the existing record; an
   UNCERTAIN id raises `DuplicateExecution`. One in-flight order per token.
2. `getLatestBlockhash` → `lastValidBlockHeight` recorded.
3. Compute budget: configured CU limit + priority fee (static or recent-fee percentile, clamped, capped).
4. `simulateTransaction` (mandatory) → error ⇒ REJECTED, never broadcast; `unitsConsumed` tunes the CU limit.
5. Sign; persist SIGNED record with the signature **before** the first broadcast.
6. Broadcast with `skipPreflight`, `maxRetries: 0`; re-broadcast the *same bytes* every ~2s while valid.
7. Confirm by polling `getSignatureStatuses`: `confirmed` + no error ⇒ CONFIRMED; with error ⇒ FAILED.
8. Block height past `lastValidBlockHeight` and still unknown with `searchTransactionHistory` ⇒ EXPIRED
   (provably not landed; a new intent may be created).
9. Cannot prove either way (RPC down, timeout) ⇒ **UNCERTAIN** ⇒ kill switch immediately. Never auto-resent.
   `TransactionEngine.reconcile()` re-checks by signature.
10. Fills are parsed from the confirmed transaction (`preBalances/postBalances`, token balances, fee).

Every state transition is written to `executions` (upsert) and `execution_events` (append-only).

Bonding-curve buys use `buy_exact_quote_in_v2` (spend exactly X SOL incl. fees, `min_tokens_out` protection);
sells use `sell_v2` and close the token account on full exit (rent refunded). PumpSwap trades wrap SOL into a
temporary wSOL account and close it in the same transaction.

**Verification status:** quote math is verified against the official SDKs; instruction account order/flags are
verified against the IDL and transaction size against the 1232-byte limit by tests. The live path has **not**
been exercised against mainnet from this repository (the build environment had no Solana RPC access). Treat
SHADOW's on-chain simulation results as the gate: do not go live until shadow simulation succeeds consistently.

## Kill switch

Triggers: daily loss limit, max drawdown, N execution failures in a window, repeated abnormal slippage, stale
data with open positions, RPC degraded beyond threshold, N database errors, UNCERTAIN transaction, wallet
balance drifting from accounting (`BalanceGuard`), runtime task crash, operator (`ta kill`, `POST /system/kill`,
dashboard). Effects: no entries, no strategy orders, state preserved, Telegram alert, optional emergency exit
(`killswitch.emergency_exit_on_kill`). The latched state is persisted (`runtime_state.killswitch`) and survives
restarts. Reset requires `confirm=RESET_KILL_SWITCH`.

## Exact procedure PAPER → SHADOW → LIVE

1. **Record & research (≥ 2–4 weeks).** `config/research.yaml`. Confirm ingestion health: gap rate, reserve
   mismatch rate, `ingest.undecodable_event` ≈ 0. Run `ta research --db`; only signals with clean placebo,
   BH-adjusted significance, stable sign across halves and positive after-cost top quintile are candidates.
2. **Walk-forward.** `ta walkforward --db`. Proceed only on `EDGE SUPPORTED`. On `NO EDGE DEMONSTRATED`, stop:
   the correct output of this system is NO TRADE.
3. **Paper (≥ 2 weeks, ≥ `backtest.min_test_trades` closed trades).** Compare paper expectancy and its CI
   with the walk-forward out-of-sample estimate. Material deterioration ⇒ back to research.
4. **Shadow (≥ 1 week)** with the real dedicated wallet funded with a small amount. Require: on-chain simulation
   success ~100%, expected vs simulated output within tolerance, alerts received, kill-switch drill done
   (`ta kill`, verify halt + alert + restart behaviour), `ta security-check` PASS.
5. **Arm LIVE with tiny limits.** Edit `config/live_limits.yaml` (e.g. `max_position_sol: 0.05`,
   `max_total_exposure_sol: 0.1`, `max_daily_loss_sol: 0.05`, `max_open_positions: 1`). Fund the wallet with no
   more than you accept losing. Set `LIVE_TRADING_CONFIRM`, start with `--config config/live.yaml`.
6. **Monitor.** Compare live fills with shadow expectations (slippage, failure rate). Any UNCERTAIN, any balance
   mismatch, any abnormal slippage ⇒ the kill switch fires; investigate before resetting.
7. **Never scale because of a winning streak.** Increase limits only after a pre-declared number of live trades
   whose expectancy CI is consistent with the walk-forward estimate, by editing `live_limits.yaml` deliberately.
