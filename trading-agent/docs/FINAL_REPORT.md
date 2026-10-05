# Final report

Status as of 2026-10-05. Strategy profitability is **not claimed**. Nothing in this repository has been run
against mainnet: the build environment had no Solana RPC access. Every result below comes from tests or from
synthetic control data, and none of it is evidence of a real edge.

## 1. What is implemented (and tested)

| Area | Status | Evidence |
|---|---|---|
| Pump bonding-curve & PumpSwap quote math (fees by market-cap tier, creator fee rules, effective quote reserves) | Implemented | 1800/1800 golden vectors from the official SDKs match lamport-for-lamport |
| IDL-driven Borsh decoding of accounts & events (incl. appended-field tolerance, emit_cpi framing) | Implemented | Unit tests; vendored IDLs at pump-public-docs `cb188ce` (2026-09-29) |
| Log parsing → normalized events; failed-tx skipping; truncation → DATA_GAP | Implemented | Unit tests |
| WebSocket ingestion with reconnect, staleness watchdog, provider rotation, gap events | Implemented | Integration test against a local WS server (drop + reconnect) |
| RPC pool with failover, circuit breakers, JSON-RPC error semantics | Implemented | Failure tests with mock transport |
| Token registry + lifecycle FSM, partial-history flagging, reserve-continuity integrity checks | Implemented | Unit tests (caught a real ordering bug in the synthetic generator) |
| Multi-window features, wallet/creator/entity analysis with confidence | Implemented | Unit tests incl. look-ahead guard |
| Risk filter, explainable score, regime detection | Implemented | Unit + integration tests |
| Empirical EV model with INSUFFICIENT DATA, risk-based sizing, execution simulator | Implemented | Unit tests |
| Decision pipeline (all gates recorded, AI veto-only) | Implemented | Integration tests (AI timeout/malformed/required/veto/positive-can't-rescue) |
| Exits: hard stop, TP ladder, trailing, time stop, thesis invalidation, lifecycle, kill-switch emergency exit | Implemented | Unit tests |
| Portfolio accounting at executable value | Implemented | Unit tests |
| Kill switch (latched, persisted, automatic triggers), pause, operator commands | Implemented | Unit, API and runtime tests |
| Transaction engine state machine (persist-before-send, EXPIRED proof, UNCERTAIN never resent) | Implemented | Scripted-RPC tests |
| Instruction builders for buy_exact_quote_in_v2 / sell_v2 / PumpSwap buy/sell | Implemented | Account order/flags vs IDL, tx size ≤ 1232 B; **not mainnet-tested** |
| Paper / Shadow / Live providers behind one interface | Implemented | Paper & failure tests; Shadow/Live need RPC + wallet |
| Event-driven backtester, walk-forward, metrics, edge verdict | Implemented | Integration tests; control runs below |
| Research mode: labeling, experiments, BH, clustered bootstrap, de-overlap, placebo, redundancy | Implemented | Control runs below |
| PostgreSQL schema, Alembic migrations, append-only triggers | Implemented | Migrations applied to PostgreSQL 16; trigger rejection tested |
| FastAPI (all requested endpoints + extras), token auth, rate limit | Implemented | API tests |
| Next.js dashboard (overview, opportunities, token detail, positions, risk/controls, research) | Implemented | Typecheck + production build; rendered and inspected against a running stack |
| Telegram alerts | Implemented | Formatting only; not sent (no bot token / Telegram egress in the build env) |
| CLI, Docker, docs | Implemented | Dockerfiles not built (no Docker daemon in the build env) |

Test suite: 693 tests (unit, integration, failure) green, including real PostgreSQL migrations/triggers.

## 2. What is simulated

* Paper fills (latency, extra adverse bps, random failures, min_out reverts). Paper orders do not move the real
  market and are never front-run.
* Backtests (same fill model). Priority fees in backtests are static (no historical fee data).
* Shadow mode simulates the real transaction via `simulateTransaction`, but never sends it.
* The synthetic market generator (tests/demos only; refused by the backtester unless explicitly allowed).

## 3. What requires API credentials

Solana RPC + WebSocket provider (ingestion, enrichment, shadow simulation, live); Anthropic API key (AI layer,
off by default); Telegram bot token/chat id (alerts); a dedicated funded wallet (shadow simulation, live).

## 4. What requires paid infrastructure

* A reliable RPC/WS provider. Public endpoints rate-limit far below Pump.fun's event rate.
* For serious latency or completeness: Geyser/gRPC (Yellowstone) streaming instead of `logsSubscribe`, plus
  Jito/priority landing infrastructure. Not implemented here; `logsSubscribe` is the documented, gap-detected
  baseline.
* PostgreSQL sized for the event stream (several million rows/day at Pump.fun volumes) with retention.

## 5. What remains experimental

* **The strategy itself.** Score weights, filter thresholds, regime thresholds and exit parameters are
  untuned defaults. Their only justification is that they are configurable and testable.
* The live instruction path (correct by construction against the IDL and SDK derivations, unproven on mainnet).
* Regime thresholds (need calibration on recorded data).
* Creator/entity heuristics (bundled-launch, funding links) — confidence-weighted, never verdicts.
* AI analyst prompt — evaluated only in forward tests.

## 6. Control experiments (synthetic data — validates machinery, not markets)

| Experiment | Result |
|---|---|
| Research on a market with no planted edge, before fixes | "Significant" ICs and "top quintile profitable after costs" — **false positives**. Root causes: overlapping labels (one token evaluated every 3s) and a generator that wasn't null. |
| Same, after de-overlapping + token-clustered bootstrap + placebo | Overlap artefact removed; residual signals traced to real constant-product convexity in the generator. Conclusion recorded: synthetic markets on the curve are not true nulls; only recorded real data counts. |
| Walk-forward, no-edge market (6h) | `INSUFFICIENT DATA`, **0 trades** (23 training samples < required) |
| Walk-forward, planted-edge market (6h) | `INSUFFICIENT DATA`: 3 out-of-sample trades averaging +124% — and the system still refused to call it an edge (< 30 trades). Correct. |

## 7. Known failure modes

| Failure | Handling | Residual risk |
|---|---|---|
| WS message drops / log truncation | DATA_GAP + reserve-mismatch detection ⇒ token rejected for 120s | Data is lost, not repaired |
| RPC outage | Failover, circuit breakers, DEGRADED regime (no entries), kill after threshold | Positions can't be exited during a total outage |
| Uncertain transaction status | UNCERTAIN ⇒ kill switch; reconcile by signature | Manual intervention required |
| Graduation while holding | Position marked STUCK, no venue until PumpSwap pool appears | Price can gap across migration |
| Rug / liquidity collapse | Thesis invalidation, hard stop on executable value, liquidity-drop exit | Exit can be far worse than the stop on thin curves |
| Database outage | Orders fail closed; batch-writer errors ⇒ kill switch | In-memory state lost on crash between flushes (market data only) |
| Off-curve token transfers | Holder features from trades only unless enrichment is on | Concentration may be understated |
| Being out-run by faster bots | Not modelled in paper/backtest | Live results will underperform simulation |
| Pump program upgrades | IDL-driven decoding; unknown events counted (`ingest.undecodable_event`) | Instruction changes require an IDL refresh and re-test |
| Python latency | Fine for second-scale signals | Uncompetitive for slot-level sniping (by design, not a goal) |

## 8. Exact procedure PAPER → SHADOW → LIVE

See [LIVE_EXECUTION.md](LIVE_EXECUTION.md#exact-procedure-paper--shadow--live). In short: record ≥2–4 weeks →
research (clean placebo, BH-significant, stable, after-cost positive) → walk-forward must say EDGE SUPPORTED
→ paper ≥2 weeks consistent with walk-forward → shadow ≥1 week with ~100% on-chain simulation success and a
kill-switch drill → LIVE with tiny `live_limits.yaml` and a wallet you can afford to empty → scale only by
deliberate edits after a pre-declared sample, never after a streak. If any step says no edge: **NO TRADE**.

## 9. Honest assessment of the idea

The engineering goal is sound and now exists. The trading premise is weak by default: Pump.fun flow is
dominated by low-latency bots, bundled launches and creator distribution, platform fees alone are roughly 2–2.5% per
round trip (≈1–1.25% per side by market-cap tier) before price impact, and most tokens die within minutes. A second-scale Python system will not win speed
races; any edge, if it exists, has to come from selection (avoiding bad tokens, sizing to exit liquidity) and
must survive costs out of sample. The system is built so that finding no edge is a successful, cheap outcome.
