"""Research-mode runner: replay events WITHOUT trading, label every candidate, run experiments."""

from __future__ import annotations

from typing import Any

from tradingagent.backtest.engine import Backtester
from tradingagent.common.config import AppConfig
from tradingagent.common.events import MarketEvent
from tradingagent.common.versions import FEATURE_VERSION, SCORING_VERSION, STRATEGY_VERSION
from tradingagent.research.experiments import feature_screen, redundancy, run_all, to_frame


def run_research(
    cfg: AppConfig, events: list[MarketEvent], allow_synthetic: bool | None = None, n_boot: int = 1000
) -> dict[str, Any]:
    bt = Backtester(cfg, calibration=False, label_outcomes=True, research_only=True, allow_synthetic=allow_synthetic)
    res = bt.run(events)
    df = to_frame(res.labels)
    min_n = cfg.research.min_samples
    experiments = run_all(df, min_samples=min_n, n_boot=n_boot)
    return {
        "data_range": {"start": res.start, "end": res.end, "events": res.n_events, "tokens": res.n_tokens},
        "synthetic_data": res.synthetic,
        "labeled_signals": len(df),
        "versions": {
            "feature": FEATURE_VERSION,
            "scoring": SCORING_VERSION,
            "strategy": STRATEGY_VERSION,
            "configuration": cfg.configuration_version,
        },
        "experiments": [r.to_dict() for r in experiments],
        "feature_screen_exec_ret_300s": feature_screen(df, "exec_ret_300s", min_n)[:40],
        "redundant_pairs": redundancy(df, 0.8, min_n)[:40],
        "note": (
            "Correlations here are hypotheses. Promote a signal to the strategy only after it survives "
            "walk-forward out-of-sample testing after costs."
        ),
        "_frame": df,
    }
