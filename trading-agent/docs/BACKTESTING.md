# Backtesting

## Data

Backtests replay **recorded** events: the worker in any mode persists every decoded event to `market_events`
(with `observed_at`, the time this system actually saw it). Export/import as JSONL:

```bash
ta export-events --out data/2026-10.jsonl --from 1790000000 --to 1792000000
```

There is no free, complete historical Pump.fun tick source; plan on recording your own for weeks before any
evaluation means anything. Synthetic data (`ta synth`) is refused unless `--allow-synthetic` is passed and is
only for testing the machinery.

## Single backtest (in-sample — diagnostic only)

```bash
ta backtest --db --from <unix> --to <unix> --out reports/bt.json
ta backtest --input data/2026-10.jsonl --out reports/bt.json
```

The report includes decisions by outcome, gate-failure counts, executions, every trade with versions, and
metrics: win rate, average winner/loser, expectancy (per trade, SOL), profit factor, max drawdown, average and
median hold, return percentiles, CVaR 5%, fees, expected vs actual slippage, failed-execution rate, bootstrap CI
of expectancy, per-regime and per-exit-reason breakdowns.

## Walk-forward (the only result that counts)

```bash
ta walkforward --db --train-days 7 --test-days 2 --step-days 2 --out reports/wf.json
```

Per fold: TRAIN replays with calibration (every signal-gate candidate becomes a virtual position followed by the
real exit policy) to collect outcome samples → optional selection of `min_score` from the small, pre-declared
`backtest.score_threshold_grid` (every grid point and its training result is reported) → TEST replays from the
start of the data in ingest-only mode up to the test window (warm-up), then trades the test window with the EV
model frozen on training samples. Only test-window trades are aggregated.

Verdict (`backtest/metrics.py: edge_verdict`):

* `INSUFFICIENT DATA` — fewer than `backtest.min_test_trades` out-of-sample trades.
* `EDGE SUPPORTED (out-of-sample, after costs)` — bootstrap 95% CI lower bound of mean net return per trade > 0
  AND profit factor > 1 AND a majority of folds positive. Still requires paper/shadow forward testing.
* `NO EDGE DEMONSTRATED` — anything else. Correct behaviour: NO TRADE.

## What the simulation models

Exact Pump/PumpSwap fees by market-cap tier, exact price impact of our own size on the venue state *after* the
latency delay (other traders' flow in between included), extra adverse drift, random transaction failures,
on-chain-style `min_out` reverts that still pay network fees, base + priority fees, executable (sell-quote)
marking, graduation freezes (no venue while GRADUATING), dead-token write-offs, end-of-data liquidation.

## What it cannot model

Competition for the same fill (faster bots / Jito bundles landing ahead of you), priority-fee auctions under
congestion, RPC/validator propagation variance beyond the configured latency, dropped events in your own
recording (gaps are flagged and gated, not reconstructed), and the AI (disabled in backtests; evaluated in
paper/shadow only). Backtest results are therefore an upper bound on what live trading can achieve.

## Anti-overfitting rules enforced by the tooling

* Train/test separation per fold with strict time ordering; test folds never see training-period outcomes from
  the future and never learn online.
* The only tunable in walk-forward is one threshold from a ≤3-point grid; configs tried are reported.
* Score weights are not optimised anywhere in the code base.
* Minimum sample sizes everywhere (`ev.min_samples`, `research.min_samples`, `backtest.min_test_trades`).
* Confidence intervals (bootstrap) and per-regime performance are always reported.
