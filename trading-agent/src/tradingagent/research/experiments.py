"""Research experiments: does a signal actually predict outcomes — after costs, out of sample?

Every experiment reports sample size, Spearman information coefficient with a bootstrap CI, the IC separately on
the first and second half of the period (stability), mean EXECUTABLE (after-cost) return by feature quintile, and
a Benjamini-Hochberg adjusted p-value across all features tested in the same run. Results are stored separately
from the production strategy. An interesting correlation is a hypothesis, not a trading rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

BUILTIN_EXPERIMENTS: list[dict[str, Any]] = [
    {
        "name": "early_holder_growth_continuation",
        "feature": "holder_growth_60s",
        "label": "exec_ret_300s",
        "question": "Does early holder growth predict continuation?",
    },
    {
        "name": "imbalance_1m_return",
        "feature": "imbalance_60s",
        "label": "exec_ret_60s",
        "question": "Does buy/sell imbalance predict 1-minute returns?",
    },
    {
        "name": "creator_history_survival",
        "feature": "creator_prev_graduation_rate",
        "label": "alive_900s",
        "question": "Does creator history matter (survival)?",
    },
    {
        "name": "creator_rug_history_return",
        "feature": "creator_prev_rug_rate",
        "label": "exec_ret_300s",
        "question": "Do prior creator rugs predict worse returns?",
    },
    {
        "name": "liquidity_growth_survival",
        "feature": "liq_change_60s",
        "label": "alive_900s",
        "question": "Does liquidity growth predict survival?",
    },
    {
        "name": "volume_acceleration_continuation",
        "feature": "vol_accel_30s",
        "label": "exec_ret_300s",
        "question": "Does volume acceleration predict continuation?",
    },
    {
        "name": "smart_wallet_share_return",
        "feature": "smart_wallet_buy_share_300s",
        "label": "exec_ret_300s",
        "question": "Does historically-profitable wallet participation predict returns?",
    },
    {
        "name": "score_after_cost",
        "feature": "__score__",
        "label": "exec_ret_300s",
        "question": "Does the composite score rank after-cost outcomes?",
    },
]


def to_frame(rows: list[dict]) -> pd.DataFrame:
    """Flatten labeled rows into one DataFrame: f__<feature>, l__<label>, plus metadata."""
    recs = []
    for r in rows:
        d = {
            "decision_id": r["decision_id"],
            "mint": r["mint"],
            "t": r["t"],
            "score": r.get("score"),
            "regime": r.get("regime"),
            "passed_stage1": r.get("passed_stage1"),
        }
        for k, v in (r.get("features") or {}).items():
            d[f"f__{k}"] = v
        for k, v in (r.get("labels") or {}).items():
            d[f"l__{k}"] = v
        recs.append(d)
    df = pd.DataFrame.from_records(recs)
    if not df.empty:
        df = df.sort_values("t").reset_index(drop=True)
        for c in [c for c in df.columns if c.startswith("l__dead_")]:
            df[c.replace("l__dead_", "l__alive_")] = (~df[c].astype(bool)).astype(float)
    return df


def horizon_of(label: str) -> float:
    """'exec_ret_300s' -> 300."""
    tail = label.rsplit("_", 1)[-1]
    return float(tail[:-1]) if tail.endswith("s") and tail[:-1].isdigit() else 0.0


def deoverlap(df: pd.DataFrame, gap_s: float) -> pd.DataFrame:
    """Keep at most one signal per token per `gap_s` so label windows do not overlap.

    Without this, a token evaluated every few seconds contributes dozens of nearly identical, overlapping labels,
    and every p-value computed on them is fiction.
    """
    if gap_s <= 0 or df.empty:
        return df
    keep = []
    last: dict[str, float] = {}
    for idx, mint, t in zip(df.index, df["mint"], df["t"], strict=True):
        if t - last.get(mint, -1e18) >= gap_s:
            keep.append(idx)
            last[mint] = t
    return df.loc[keep]


def _cluster_boot(
    x: np.ndarray, y: np.ndarray, groups: np.ndarray, n_boot: int, seed: int
) -> tuple[tuple[float, float] | None, float | None]:
    """Token-clustered bootstrap of Spearman IC -> (95% CI, two-sided p-value). Resamples TOKENS, not rows."""
    uniq = np.unique(groups)
    if len(uniq) < 20:
        return None, None
    rng = np.random.default_rng(seed)
    rx, ry = stats.rankdata(x), stats.rankdata(y)
    idx_by_group = {g: np.flatnonzero(groups == g) for g in uniq}
    vals = []
    for _ in range(n_boot):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([idx_by_group[g] for g in pick])
        a, b = rx[idx], ry[idx]
        if a.std() == 0 or b.std() == 0:
            continue
        vals.append(np.corrcoef(a, b)[0, 1])
    if len(vals) < 50:
        return None, None
    v = np.asarray(vals)
    p = float(min(1.0, 2 * min((v <= 0).mean(), (v >= 0).mean())))
    return (float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))), max(p, 1.0 / len(v))


def _spearman(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    if len(x) < 3 or np.all(x == x[0]) or np.all(y == y[0]):
        return float("nan"), float("nan")
    r = stats.spearmanr(x, y)
    return float(r.statistic), float(r.pvalue)


def benjamini_hochberg(pvals: list[float]) -> list[float]:
    n = len(pvals)
    order = sorted(range(n), key=lambda i: math.inf if math.isnan(pvals[i]) else pvals[i])
    adj = [math.nan] * n
    prev = 1.0
    for rank in range(n, 0, -1):
        i = order[rank - 1]
        p = pvals[i]
        if math.isnan(p):
            continue
        prev = min(prev, p * n / rank)
        adj[i] = prev
    return adj


@dataclass
class ExperimentResult:
    name: str
    question: str
    feature: str
    label: str
    n: int
    ic: float | None
    p_value: float | None
    ic_ci95: tuple[float, float] | None
    ic_first_half: float | None
    ic_second_half: float | None
    quintile_mean_label: list[float | None]
    top_minus_bottom: float | None
    top_quintile_hit_rate: float | None
    p_adjusted: float | None = None
    n_tokens: int = 0
    n_raw: int = 0
    placebo_ic: float | None = None
    placebo_p: float | None = None
    status: str = "OK"
    interpretation: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def run_experiment(
    df: pd.DataFrame,
    name: str,
    feature: str,
    label: str,
    question: str = "",
    min_samples: int = 200,
    n_boot: int = 1000,
    seed: int = 0,
) -> ExperimentResult:
    fcol = "score" if feature == "__score__" else f"f__{feature}"
    lcol = f"l__{label}"
    empty = ExperimentResult(name, question, feature, label, 0, None, None, None, None, None, [], None, None)
    if fcol not in df or lcol not in df:
        empty.status = "MISSING_COLUMNS"
        return empty
    raw = df[[fcol, lcol, "t", "mint"]].copy()
    raw[[fcol, lcol]] = raw[[fcol, lcol]].apply(pd.to_numeric, errors="coerce")
    raw = raw.dropna()
    sub = deoverlap(raw.sort_values("t"), horizon_of(label))
    n = len(sub)
    if n < min_samples:
        empty.n, empty.n_raw, empty.status = n, len(raw), f"INSUFFICIENT_DATA (n={n} non-overlapping < {min_samples})"
        return empty
    x, y = sub[fcol].to_numpy(float), sub[lcol].to_numpy(float)
    groups = sub["mint"].to_numpy()
    ic, _ = _spearman(x, y)
    ci, p = _cluster_boot(x, y, groups, n_boot, seed)
    half = n // 2
    ic1, _ = _spearman(x[:half], y[:half])
    ic2, _ = _spearman(x[half:], y[half:])
    q: list[float | None] = []
    top_hit = None
    try:
        bins = pd.qcut(sub[fcol].rank(method="first"), 5, labels=False)
        q = [float(sub[lcol][bins == i].mean()) for i in range(5)]
        top = sub[lcol][bins == 4]
        top_hit = float((top > 0).mean()) if len(top) else None
    except ValueError:
        q = []
    res = ExperimentResult(
        name, question, feature, label, n, ic, p, ci, ic1, ic2, q, (q[-1] - q[0]) if len(q) == 5 else None, top_hit
    )
    res.n_tokens, res.n_raw = len(np.unique(groups)), len(raw)
    # Placebo: permute the feature ACROSS TOKENS (whole-token blocks), keeping each token's internal structure.
    # The same pipeline must find nothing here; if it does, the method (not the market) is producing the signal.
    rng = np.random.default_rng(seed + 7919)
    uniq = np.unique(groups)
    perm = dict(zip(uniq, rng.permutation(uniq), strict=True))
    by_group = {g: x[groups == g] for g in uniq}
    xp = np.empty_like(x)
    for g in uniq:
        src = by_group[perm[g]]
        idx = np.flatnonzero(groups == g)
        xp[idx] = np.resize(src, len(idx))
    res.placebo_ic, _ = _spearman(xp, y)
    _, res.placebo_p = _cluster_boot(xp, y, groups, max(100, n_boot // 2), seed + 1)
    return res


def interpret(r: ExperimentResult) -> str:
    if r.status != "OK" or r.ic is None or math.isnan(r.ic):
        return r.status
    sig = r.p_adjusted is not None and r.p_adjusted < 0.05
    stable = (
        r.ic_first_half is not None
        and r.ic_second_half is not None
        and not math.isnan(r.ic_first_half)
        and not math.isnan(r.ic_second_half)
        and np.sign(r.ic_first_half) == np.sign(r.ic_second_half)
    )
    ci_excl_0 = r.ic_ci95 is not None and (r.ic_ci95[0] > 0 or r.ic_ci95[1] < 0)
    profitable_top = r.label.startswith("exec_ret") and r.quintile_mean_label and (r.quintile_mean_label[-1] or 0) > 0
    placebo_bad = r.placebo_p is not None and r.placebo_p < 0.05
    parts = [
        f"IC={r.ic:+.3f}",
        "significant after BH correction" if sig else "not significant after BH correction",
        "PLACEBO ALSO SIGNIFICANT (method suspect)" if placebo_bad else "placebo clean",
        "same sign in both halves" if stable else "UNSTABLE across halves",
        "CI excludes 0" if ci_excl_0 else "CI includes 0",
    ]
    if r.label.startswith("exec_ret"):
        parts.append(
            "top quintile positive AFTER costs" if profitable_top else "top quintile NOT profitable after costs"
        )
    return "; ".join(parts)


def run_all(
    df: pd.DataFrame, experiments: list[dict] | None = None, min_samples: int = 200, n_boot: int = 1000
) -> list[ExperimentResult]:
    exps = experiments or BUILTIN_EXPERIMENTS
    res = [
        run_experiment(df, e["name"], e["feature"], e["label"], e.get("question", ""), min_samples, n_boot, i)
        for i, e in enumerate(exps)
    ]
    adj = benjamini_hochberg([r.p_value if r.p_value is not None else math.nan for r in res])
    for r, a in zip(res, adj, strict=True):
        r.p_adjusted = None if math.isnan(a) else a
        r.interpretation = interpret(r)
    return res


def feature_screen(
    df: pd.DataFrame, label: str = "exec_ret_300s", min_samples: int = 200, n_boot: int = 300
) -> list[dict]:
    """IC of every numeric feature vs an after-cost label on NON-OVERLAPPING samples, token-clustered p-values,
    BH-adjusted: 'which features have predictive power after fees?'"""
    lcol = f"l__{label}"
    if lcol not in df:
        return []
    out = []
    for c in [c for c in df.columns if c.startswith("f__")]:
        r = run_experiment(df, c[3:], c[3:], label, "", min_samples, n_boot, 0)
        if r.status != "OK" or r.ic is None or math.isnan(r.ic):
            continue
        out.append(
            {
                "feature": c[3:],
                "n": r.n,
                "tokens": r.n_tokens,
                "ic": r.ic,
                "p": r.p_value,
                "ic_ci95": r.ic_ci95,
                "ic_first_half": r.ic_first_half,
                "ic_second_half": r.ic_second_half,
                "top_quintile_mean": r.quintile_mean_label[-1] if r.quintile_mean_label else None,
            }
        )
    adj = benjamini_hochberg([r["p"] if r["p"] is not None else math.nan for r in out])
    for r, a in zip(out, adj, strict=True):
        r["p_adjusted"] = None if math.isnan(a) else a
        r["stable_sign"] = (
            r["ic_first_half"] is not None
            and r["ic_second_half"] is not None
            and not math.isnan(r["ic_first_half"])
            and not math.isnan(r["ic_second_half"])
            and np.sign(r["ic_first_half"]) == np.sign(r["ic_second_half"])
        )
    out.sort(key=lambda r: -abs(r["ic"]))
    return out


def redundancy(df: pd.DataFrame, threshold: float = 0.8, min_samples: int = 200) -> list[dict]:
    """Pairs of features with |Spearman rho| >= threshold — candidates for removal ('which signals are redundant?')."""
    cols = [c for c in df.columns if c.startswith("f__")]
    num = df[cols].apply(pd.to_numeric, errors="coerce")
    num = num.loc[:, num.notna().sum() >= min_samples]
    num = num.loc[:, num.std(skipna=True) > 0]
    if num.shape[1] < 2:
        return []
    corr = num.rank().corr()
    out = []
    names = list(corr.columns)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            v = corr.loc[a, b]
            if not math.isnan(v) and abs(v) >= threshold:
                out.append({"a": a[3:], "b": b[3:], "rho": float(v)})
    out.sort(key=lambda r: -abs(r["rho"]))
    return out
