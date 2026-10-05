"""Walk-forward evaluation: TRAIN -> TEST -> MOVE WINDOW -> TRAIN -> TEST ...

Per fold:
  1. TRAIN  — replay [train_start, train_end) with calibration on. Every candidate that passes the signal gates
              becomes a virtual position; its realized outcome (gross, spot-to-spot) is a training sample. No
              parameter ever sees test-period data.
  2. SELECT — optionally choose min_score from a SMALL pre-declared grid (config.backtest.score_threshold_grid)
              by training-sample expectancy after a representative cost. Every grid point tried is reported, so
              the number of configurations tested is never hidden.
  3. TEST   — replay from the beginning of data in ingest-only mode up to test_start (warm-up: creator history,
              wallet stats, token state), then trade [test_start, test_end) with the EV model FROZEN on training
              samples. Only test-period trades count.
Aggregate out-of-sample metrics and an edge verdict are produced at the end.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from tradingagent.backtest.engine import Backtester, FrozenOutcomeStore
from tradingagent.backtest.metrics import edge_verdict, performance
from tradingagent.common.config import AppConfig
from tradingagent.common.events import MarketEvent
from tradingagent.common.logging import get_logger
from tradingagent.strategy.ev import OutcomeSample, OutcomeStore

log = get_logger("backtest.walkforward")
DAY = 86_400.0


@dataclass
class Fold:
    index: int
    train: tuple[float, float]
    test: tuple[float, float]
    n_train_samples: int = 0
    grid: list[dict] = field(default_factory=list)
    chosen_min_score: float | None = None
    test_metrics: dict = field(default_factory=dict)
    test_trades: list[dict] = field(default_factory=list)
    decisions: dict = field(default_factory=dict)
    rejections: dict = field(default_factory=dict)


def make_folds(start: float, end: float, train_s: float, test_s: float, step_s: float) -> list[Fold]:
    folds, s, i = [], start, 0
    while s + train_s + test_s <= end + 1e-9:
        folds.append(Fold(i, (s, s + train_s), (s + train_s, s + train_s + test_s)))
        s += step_s
        i += 1
    return folds


def select_threshold(
    samples: list[OutcomeSample], grid: list[float], cost: float, min_samples: int
) -> tuple[float | None, list[dict]]:
    report = []
    best, best_exp = None, -1e9
    for thr in grid:
        xs = [s.gross_return - cost for s in samples if s.score >= thr]
        row = {"min_score": thr, "n": len(xs), "train_expectancy_after_cost": statistics.mean(xs) if xs else None}
        report.append(row)
        if (
            len(xs) >= min_samples
            and row["train_expectancy_after_cost"] is not None
            and row["train_expectancy_after_cost"] > best_exp
        ):
            best, best_exp = thr, row["train_expectancy_after_cost"]
    return best, report


def run_walkforward(
    cfg: AppConfig,
    events: list[MarketEvent],
    train_days: float | None = None,
    test_days: float | None = None,
    step_days: float | None = None,
    seed: int = 0,
    allow_synthetic: bool | None = None,
    select: bool = True,
    representative_cost: float = 0.04,
) -> dict[str, Any]:
    bc = cfg.backtest
    train_s = (train_days if train_days is not None else bc.train_days) * DAY
    test_s = (test_days if test_days is not None else bc.test_days) * DAY
    step_s = (step_days if step_days is not None else bc.step_days) * DAY
    events = sorted(events, key=lambda e: e.order_key)
    if not events:
        return {"error": "no events"}
    start, end = events[0].t, events[-1].t
    folds = make_folds(start, end, train_s, test_s, step_s)
    if not folds:
        return {"error": f"data span {(end - start) / DAY:.2f}d shorter than one train+test window"}
    all_test_trades: list[dict] = []
    fold_exp: list[float] = []
    for f in folds:
        train_events = [e for e in events if e.t < f.train[1]]  # earlier data = ingest-only warm-up
        bt = Backtester(
            cfg,
            outcomes=OutcomeStore(),
            seed=seed + f.index,
            calibration=True,
            trade_start=f.train[0],
            allow_synthetic=allow_synthetic,
        )
        tr = bt.run(train_events)
        samples = [s for s in tr.outcomes if s.t >= f.train[0]]
        f.n_train_samples = len(samples)
        test_cfg = cfg
        if select:
            thr, grid = select_threshold(samples, bc.score_threshold_grid, representative_cost, cfg.ev.min_samples)
            f.grid = grid
            if thr is not None:
                f.chosen_min_score = thr
                test_cfg = cfg.with_overrides(strategy={"min_score": thr})
        frozen = FrozenOutcomeStore()
        frozen.samples.extend(samples)
        test_events = [e for e in events if e.t < f.test[1]]
        te = Backtester(
            test_cfg,
            outcomes=frozen,
            seed=seed + 1000 + f.index,
            calibration=False,
            trade_start=f.test[0],
            allow_synthetic=allow_synthetic,
        ).run(test_events)
        f.test_trades = [t.to_dict() for t in te.trades if t.opened_at >= f.test[0]]
        f.test_metrics = performance(
            f.test_trades,
            [p for p in te.equity_curve if p[0] >= f.test[0]],
            te.executions,
            cfg.backtest.bootstrap_samples,
        )
        f.decisions, f.rejections = te.decisions, te.rejections
        all_test_trades += f.test_trades
        if f.test_trades:
            fold_exp.append(f.test_metrics.get("expectancy_per_trade", 0.0))
        log.info(
            "walkforward_fold",
            fold=f.index,
            train_samples=f.n_train_samples,
            chosen_min_score=f.chosen_min_score,
            test_trades=len(f.test_trades),
        )
    oos = performance(all_test_trades, None, None, cfg.backtest.bootstrap_samples)
    return {
        "configuration_version": cfg.configuration_version,
        "data_range": {"start": start, "end": end, "days": (end - start) / DAY},
        "windows": {"train_days": train_s / DAY, "test_days": test_s / DAY, "step_days": step_s / DAY},
        "configs_tried_per_fold": len(bc.score_threshold_grid) if select else 1,
        "folds": [
            {
                "index": f.index,
                "train": f.train,
                "test": f.test,
                "train_samples": f.n_train_samples,
                "grid": f.grid,
                "chosen_min_score": f.chosen_min_score,
                "test_metrics": f.test_metrics,
                "decisions": f.decisions,
                "rejections": f.rejections,
            }
            for f in folds
        ],
        "out_of_sample": oos,
        "verdict": edge_verdict(oos, bc.min_test_trades, fold_exp),
        "synthetic_data": any(e.source == "synthetic" for e in events[:1000]),
    }
