# Strategy research

Research mode never trades. It answers one question: **does a signal predict outcomes after costs, out of
sample?** Results are stored in `research_experiments` / `signal_outcomes`, separate from the strategy.

```bash
ta --config config/research.yaml worker --source ws    # live: label every candidate into signal_outcomes
ta research --db --out reports/research.json --csv reports/labels.csv   # offline over recorded events
```

## Labels (`research/labeling.py`)

For every evaluated candidate, at horizons 30s / 1m / 5m / 15m:

* `spot_ret_*` — spot price return
* `exec_ret_*` — **executable round trip**: buy `research.label_notional_sol` at signal time on the exact venue
  state, sell those tokens at the horizon state (fees + both legs' impact). This is the label that matters.
* `mfe_*`, `mae_*` — max favourable / adverse spot excursion
* `dead_*` / `alive_*`, `graduated_*`

Labels are finalised only after the horizon has elapsed.

## Built-in experiments (`research/experiments.py`)

| Question | Feature → label |
|---|---|
| Does early holder growth predict continuation? | `holder_growth_60s` → `exec_ret_300s` |
| Does buy/sell imbalance predict 1-minute returns? | `imbalance_60s` → `exec_ret_60s` |
| Does creator history matter? | `creator_prev_graduation_rate` → `alive_900s`; `creator_prev_rug_rate` → `exec_ret_300s` |
| Does liquidity growth predict survival? | `liq_change_60s` → `alive_900s` |
| Does volume acceleration predict continuation? | `vol_accel_30s` → `exec_ret_300s` |
| Do historically profitable wallets predict returns? | `smart_wallet_buy_share_300s` → `exec_ret_300s` |
| Does the composite score rank after-cost outcomes? | score → `exec_ret_300s` |
| Which signals are redundant? | pairwise Spearman ≥ 0.8 |
| Which features have predictive power after fees? | every feature → `exec_ret_300s`, BH-adjusted |

## Statistical safeguards (each was added because a control experiment caught its absence)

1. **De-overlapping.** A token evaluated every 3s produces dozens of overlapping labels. Only one signal per
   token per label horizon is kept. Before this fix, a null market produced "significant" results.
2. **Token-clustered bootstrap.** CIs and p-values resample tokens, not rows.
3. **Benjamini–Hochberg** correction across every test in a run.
4. **Stability.** IC computed on the first and second half of the period separately; sign flips are flagged.
5. **Placebo.** The feature is permuted across tokens (whole-token blocks). If the placebo is also
   "significant", the method — not the market — is producing the signal.
6. **After-cost framing.** Quintile means are reported on the executable label; "top quintile NOT profitable
   after costs" is stated explicitly.
7. **Minimum samples** (`research.min_samples`, default 200 non-overlapping signals) ⇒ otherwise
   `INSUFFICIENT_DATA`.

## Control experiments run during development (synthetic data — machinery validation only)

* Synthetic market with **no** planted edge: before fixes 1–2, imbalance/holder-growth/score showed BH-significant
  ICs with "top quintile positive after costs". After the fixes, the null still shows some significant ICs —
  because a constant-product bonding curve has *real mechanical structure* (convexity: buying and selling equal
  SOL amounts is not price-neutral), not because of leakage. Lesson recorded: any synthetic market built on the
  curve is not a true null, so synthetic "edges" are artifacts; only recorded real data counts, and the placebo
  test is the methodology check.
* Walk-forward on the null market: `INSUFFICIENT DATA`, 0 trades — the EV gate refused to trade without
  outcome evidence.

## Promotion rule

A signal moves into `scoring.components` only after: clean placebo, BH-significant, stable sign, positive
after-cost top quintile, **and** a walk-forward verdict of EDGE SUPPORTED with it included. Changing scoring
must bump `SCORING_VERSION` and `STRATEGY_VERSION`; the EV model only uses outcomes whose strategy version matches
`ev.use_strategy_version_prefix`, so outcomes of an old strategy cannot justify trades of a new one.
