# Paper and shadow trading

## Paper (Phase 4)

```bash
ta --config config/paper.yaml worker --source ws
```

Live market data, the exact live decision path (same `TradingCore`), hypothetical fills from
`PaperFillModel`: the order waits `execution.assumed_latency_s`, is quoted against the venue state at that
moment, pays exact fees plus `paper_extra_adverse_bps`, fails randomly at `paper_failure_rate`, and reverts like
the chain does if the result is below `min_out` (network fees still charged). Positions are marked at the exact
sell quote for their full size.

The dashboard shows PAPER EQUITY, open paper positions, P/L, win rate, expectancy, drawdown and the full
closed-trade metrics. Reset with `ta paper-reset` or `POST /paper/reset` (history is kept; a new session starts).

### Bootstrapping the EV model

With no outcome history every candidate is `NO_TRADE: INSUFFICIENT DATA`. That is intended. While running,
every candidate that passes the signal gates (data, filters, score, regime) opens a **calibration position**:
no capital, same exit policy, outcome recorded to `outcome_samples` with `source=calibration`. Once a score
bucket has `ev.min_samples` outcomes the EV gate starts evaluating it. Use `ev.outcome_source` to restrict which
sources count (e.g. only `shadow` before going live).

## Shadow (Phase 5)

```bash
WALLET_KEYPAIR_PATH=/secure/trading.json SOLANA_RPC_URLS=... ta --config config/shadow.yaml worker --source ws
```

Everything paper does, plus: for every order the **real** transaction is built (IDL-driven accounts, real fee
recipients from the on-chain Global/GlobalConfig, real pool accounts) and sent to `simulateTransaction` with the
real wallet. Nothing is ever broadcast (`TransactionEngine.execute(dry_run=True)` stops after simulation). A
failed on-chain simulation turns the hypothetical fill into a FAILED execution, and the log line
`shadow_would_send` records exactly what would have been sent.

Shadow is the final validation stage: run it until (a) on-chain simulation success rate is ~100%, (b) expected
vs actual slippage is calibrated, (c) forward outcomes support the EV estimates, (d) the kill switch and
alerts have been drilled.

## Known paper/shadow limitations

* Hypothetical orders do not move the real market and are never front-run; real fills will be worse.
* The latency is a constant; real landing time varies with priority fee and congestion.
* Simulation success does not guarantee landing.
