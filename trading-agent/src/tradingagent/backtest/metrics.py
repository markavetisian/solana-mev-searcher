"""Performance metrics. Optimise risk-adjusted expectancy, not win rate.

Reports win rate AND average winner/loser, expectancy, profit factor, drawdown, holding times, return
distribution, tail loss (CVaR 5%), fees, slippage, failed-execution rate, and a bootstrap confidence interval for
mean net return per trade.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from typing import Any


def bootstrap_mean_ci(
    xs: list[float], n_boot: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float] | None:
    if len(xs) < 5:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(n)) / n for _ in range(n_boot))
    return means[int(alpha / 2 * n_boot)], means[int((1 - alpha / 2) * n_boot) - 1]


def max_drawdown(equity: list[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, 1 - e / peak)
    return mdd


def performance(
    trades: list[dict],
    equity_curve: list[tuple[float, float]] | None = None,
    executions: dict | None = None,
    n_boot: int = 2000,
) -> dict[str, Any]:
    n = len(trades)
    out: dict[str, Any] = {"trades": n}
    if executions:
        tot = executions.get("total", 0)
        out["executions"] = executions
        out["failed_execution_rate"] = (executions.get("failed", 0) / tot) if tot else None
    if equity_curve:
        eq = [e for _, e in equity_curve]
        out["max_drawdown"] = max_drawdown(eq)
        out["start_equity_sol"], out["end_equity_sol"] = eq[0], eq[-1]
        out["total_return"] = eq[-1] / eq[0] - 1 if eq[0] else None
    if n == 0:
        out["verdict_hint"] = "no trades"
        return out
    rets = [t["net_return"] for t in trades]
    pnl = [t["pnl_lamports"] / 1e9 for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_win = sum(p for p in pnl if p > 0)
    gross_loss = -sum(p for p in pnl if p <= 0)
    holds = [t["hold_s"] for t in trades]
    srt = sorted(rets)
    k = max(1, int(0.05 * n))
    out.update(
        {
            "win_rate": len(wins) / n,
            "avg_winner": statistics.mean(wins) if wins else 0.0,
            "avg_loser": statistics.mean(losses) if losses else 0.0,
            "expectancy_per_trade": statistics.mean(rets),
            "expectancy_sol_per_trade": statistics.mean(pnl),
            "median_return": statistics.median(rets),
            "total_pnl_sol": sum(pnl),
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else None),
            "avg_hold_s": statistics.mean(holds),
            "median_hold_s": statistics.median(holds),
            "return_pctiles": {p: srt[min(n - 1, int(p / 100 * n))] for p in (1, 5, 25, 50, 75, 95, 99)},
            "cvar_5": statistics.mean(srt[:k]),
            "worst_trade": srt[0],
            "best_trade": srt[-1],
            "fees_sol": sum((t["network_fees"] + t["platform_fees"]) / 1e9 for t in trades),
            "avg_expected_entry_slippage": statistics.mean(t["expected_entry_slippage"] for t in trades),
            "avg_actual_entry_slippage": statistics.mean(t["actual_entry_slippage"] for t in trades),
            "expectancy_ci95": bootstrap_mean_ci(rets, n_boot),
            "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
        }
    )
    by_regime: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_regime[t["regime"]].append(t["net_return"])
    out["by_regime"] = {
        r: {"n": len(v), "expectancy": statistics.mean(v), "win_rate": sum(x > 0 for x in v) / len(v)}
        for r, v in by_regime.items()
    }
    return out


def edge_verdict(oos: dict, min_trades: int, fold_expectancies: list[float]) -> dict:
    n = oos.get("trades", 0)
    if n < min_trades:
        return {"verdict": "INSUFFICIENT DATA", "detail": f"{n} out-of-sample trades < {min_trades} required"}
    ci = oos.get("expectancy_ci95")
    pf = oos.get("profit_factor") or 0
    pos_folds = sum(1 for e in fold_expectancies if e > 0)
    checks = {
        "ci95_lower_bound_positive": bool(ci and ci[0] > 0),
        "profit_factor_above_1": pf > 1,
        "majority_of_folds_positive": pos_folds >= max(1, math.ceil(len(fold_expectancies) / 2)),
    }
    if all(checks.values()):
        return {
            "verdict": "EDGE SUPPORTED (out-of-sample, after costs)",
            "checks": checks,
            "detail": "Still requires live forward testing; backtests cannot model competition for fills.",
        }
    return {"verdict": "NO EDGE DEMONSTRATED", "checks": checks, "detail": "Correct behaviour: NO TRADE."}
