# Architecture

## Principle

The AI is not the execution authority. Deterministic code owns every decision that can move money; the AI can
only **veto**. Every stage below is a plain Python object shared verbatim by the live runtime and the backtester
(`strategy/core.py: TradingCore`). Only three things differ between PAPER, SHADOW, LIVE and BACKTEST: the clock,
the event source and the execution provider.

## Diagram

```text
                      ┌──────────────────────── Solana ────────────────────────┐
                      │ Pump program 6EF8…F6P        PumpSwap pAMMBay…EA        │
                      └───────────┬──────────────────────────────┬──────────────┘
           logsSubscribe (WS, N providers, reconnect+gap events)  │ HTTP RPC pool (failover, circuit breakers)
                                  │                               │  getAccountInfo / simulate / send / status
                                  ▼                               │
   ingestion/parser.py  IDL-driven Borsh decode (vendored official IDLs) ─► MarketEvent (normalized, timestamped)
                                  │                                                          │
                                  ▼                                                          ▼
   discovery/registry.py  TokenRecord + lifecycle FSM                         storage: market_events (append-only)
   NEW → BONDING_CURVE → GRADUATING → PUMPSWAP → ACTIVE → DEAD/UNTRADEABLE/EXITED          ▲ replay
                                  │                                                          │
   market/engine.py  per-token TradeSeries (prefix sums), venue state (curve | pool),  ◄─────┘ backtest/research
                     holder ledger, creator tracking, reserve-continuity integrity checks
                                  │
   wallets/entities.py  WalletBook (P/L, win rate, holding time), CreatorBook, EntityGraph (linked wallets w/ confidence)
                                  │
   features/engine.py   5s…1h windows: momentum, volume, order-flow proxies, liquidity/exit cost, holders, creator
                                  │
   ┌──────────────────────────────┴─── strategy/decision.py (gates, all recorded) ─────────────────────────────┐
   │ VALIDATE DATA → RISK FILTER → FEATURES → QUANT SCORE → REGIME → AI (veto-only) → EXPECTED VALUE →          │
   │ POSITION SIZE → EXECUTION SIMULATION → RISK ENGINE → APPROVE / REJECT / NO_TRADE                            │
   └──────────────────────────────┬──────────────────────────────────────────────────────────────────────────────┘
                                  │ OrderIntent (persisted BEFORE execution; DB down ⇒ no order)
                                  ▼
   execution/providers.py   ExecutionProvider ── Paper (fill model)  │ Shadow (fill model + real simulateTransaction,
                                                                     │          never broadcast)
                                                                     │ Live (TransactionEngine, independent live limits)
                                  │
   solana/tx.py  CREATED→SIMULATED→SIGNED(persisted)→SUBMITTED→CONFIRMED|FAILED|EXPIRED|UNCERTAIN (never auto-resent)
                                  │
   portfolio/portfolio.py  positions marked at EXECUTABLE value (exact sell quote incl. own impact + fees)
   strategy/exits.py       hard stop, TP ladder, trailing, time stop, thesis invalidation, lifecycle
   risk/killswitch.py      latched kill switch (persisted), pause, automatic triggers
                                  │
   storage (PostgreSQL, append-only audit) ── api (FastAPI) ── dashboard (Next.js, server-side proxy) ── Telegram
```

## Modules

| Path | Responsibility |
|---|---|
| `pump/borsh.py` | Generic Borsh decoder driven by the vendored Pump IDLs (`pump/idl/*.json`, pump-public-docs @ `cb188ce`). Tolerates appended fields exactly as the docs specify. |
| `pump/curve.py`, `pump/amm.py`, `pump/fees.py` | Exact integer quote math. Verified lamport-for-lamport against `@pump-fun/pump-sdk` 2.0.0 and `@pump-fun/pump-swap-sdk` 1.20.0 (1800 golden vectors, `tests/fixtures/pump_sdk_golden.json`, generator `scripts/gen_pump_golden.js`). |
| `pump/adapters.py` | `BondingCurveAdapter` / `PumpSwapAdapter` behind one `MarketAdapter` interface. |
| `pump/instructions.py` | Real `buy_exact_quote_in_v2` / `sell_v2` / PumpSwap `buy_exact_quote_in` / `sell` instructions, account lists built by name from the IDL. |
| `solana/rpc.py`, `solana/ws.py` | Multi-provider RPC with health scoring and circuit breakers; WS subscriber with staleness watchdog, rotation, explicit gap events. |
| `solana/tx.py` | Transaction state machine (see `LIVE_EXECUTION.md`). |
| `ingestion/` | Log parsing, sources (WS, JSONL replay, paced list), JSONL archiver, synthetic generator (tests/demos only). |
| `discovery/registry.py` | TokenRecord and lifecycle FSM with recorded transitions. |
| `market/` | Market data engine, trade series, per-token state, global activity. |
| `features/`, `scoring/`, `risk/filters.py`, `strategy/regime.py` | Research engine. |
| `strategy/ev.py`, `risk/sizing.py`, `execution/simulator.py`, `risk/engine.py`, `strategy/decision.py`, `strategy/exits.py` | Decision engine. |
| `strategy/core.py` | `TradingCore`: the single decision path used everywhere. |
| `paper/runtime.py` | Async runtime for RESEARCH/PAPER/SHADOW/LIVE. |
| `backtest/` | Event-driven backtester, walk-forward, metrics, edge verdict. |
| `research/` | Outcome labeling, experiments (IC, quintiles, BH correction, placebo), redundancy. |
| `ai/` | Strict schema, injection-hardened prompt, guarded Anthropic analyst. |
| `storage/` | SQLAlchemy models, batch writer, DB sink, replay loader. |
| `api/`, `cli/`, `worker/` | FastAPI app, `ta` CLI, runtime factory. |
| `apps/dashboard` | Next.js dashboard. |

## Look-ahead prevention (structural, not by convention)

1. Replay is strictly ordered by `(observed_at, slot, signature, seq)`; `ManualClock` refuses to move backwards.
2. The engine only holds state from events already replayed. There is no "read ahead" API.
3. `TradeSeries.window()` takes a mandatory `as_of`; `FeatureEngine.compute()` raises `LookAheadError` if the
   token's state already contains events after `as_of` (tested).
4. Orders fill `assumed_latency_s` later, against the venue state at that later time.
5. EV outcomes are queried with `t <= now`; walk-forward test folds use a frozen store built only from the
   training fold.
6. Research labels are finalised only after the label horizon has elapsed in replay time.
7. Holder/funding enrichment from RPC is recorded as timestamped events, so a replay sees exactly what live saw.
8. Survivorship: the raw stream includes every token, dead or alive; tokens first seen mid-life are flagged
   `partial_history` and rejected by default.

## Known structural limits

* Holder balances are trade-derived. Off-curve transfers are invisible without `getTokenLargestAccounts`
  enrichment (`tracking.holder_enrichment_enabled`, RPC cost).
* `logsSubscribe` can drop messages and truncate long logs. Both are detected (reserve-continuity check,
  `Log truncated` ⇒ DATA_GAP) and gate entries, but data is lost, not repaired. Geyser/gRPC is the paid fix.
* Paper fills do not move the real market and cannot model being front-run by faster bots.
