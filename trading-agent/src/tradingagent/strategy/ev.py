"""Empirical expected-value model.

    EV = P(win) * avg_win - P(loss) * avg_loss - network_cost - execution_risk

P(win), avg_win and avg_loss come ONLY from recorded outcomes of this strategy family (forward-tested paper /
shadow / calibration trades, or the training fold of a walk-forward run) — never from AI confidence.

Outcomes are stored as *gross* returns (spot at entry fill -> spot at exit fill, i.e. before fees and impact),
so they can be re-costed for each candidate's own size and liquidity:
    win  := gross_return > candidate_roundtrip_cost
    avg_win / avg_loss are measured net of that candidate cost.
The gate uses a conservative EV built from the Wilson lower bound of P(win). Fewer than `min_samples`
comparable outcomes => INSUFFICIENT_DATA => no trade.
"""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass, field

from tradingagent.common.config import EVConfig
from tradingagent.common.types import Regime
from tradingagent.common.versions import EV_MODEL_VERSION
from tradingagent.wallets.entities import wilson_lower


@dataclass(frozen=True)
class OutcomeSample:
    t: float  # entry time
    score: float
    regime: str
    gross_return: float
    net_return: float  # realized/hypothetical net of the trade's own costs (reporting only)
    hold_s: float
    source: str  # paper | shadow | live | backtest | calibration
    strategy_version: str


@dataclass
class EVEstimate:
    status: str  # OK | INSUFFICIENT_DATA
    n: int
    p_win: float | None = None
    p_win_lower: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    ev: float | None = None
    ev_conservative: float | None = None
    cost_frac: float = 0.0
    other_cost_frac: float = 0.0
    execution_risk_frac: float = 0.0
    bucket: str = ""
    conditioned_on_regime: bool = False
    version: str = EV_MODEL_VERSION
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class OutcomeStore:
    def __init__(self, max_samples: int = 100_000) -> None:
        self.samples: deque[OutcomeSample] = deque(maxlen=max_samples)

    def add(self, s: OutcomeSample) -> None:
        self.samples.append(s)

    def extend(self, items: list[OutcomeSample]) -> None:
        for s in items:
            self.add(s)

    def __len__(self) -> int:
        return len(self.samples)

    def query(
        self, now: float, lookback_s: float, version_prefix: str, source: str, lo: float, hi: float, regime: str | None
    ) -> list[OutcomeSample]:
        cut = now - lookback_s
        out = []
        for s in self.samples:
            if s.t < cut or s.t > now:  # never use outcomes of trades entered after `now`
                continue
            if not s.strategy_version.startswith(version_prefix):
                continue
            if source != "any" and s.source != source and not (source == "paper" and s.source == "calibration"):
                continue
            if not (lo <= s.score < hi):
                continue
            if regime is not None and s.regime != regime:
                continue
            out.append(s)
        return out


class EVModel:
    def __init__(self, cfg: EVConfig, store: OutcomeStore) -> None:
        self.cfg, self.store = cfg, store

    def bucket_for(self, score: float) -> tuple[float, float]:
        b = self.cfg.score_buckets
        i = max(0, min(len(b) - 2, bisect.bisect_right(b, score) - 1))
        return b[i], b[i + 1]

    def estimate(
        self,
        score: float,
        regime: Regime,
        now: float,
        roundtrip_cost_frac: float,
        other_cost_frac: float = 0.0,
        execution_risk_frac: float = 0.0,
    ) -> EVEstimate:
        c = self.cfg
        lo, hi = self.bucket_for(score)
        lookback = c.outcome_lookback_days * 86_400
        samples = self.store.query(now, lookback, c.use_strategy_version_prefix, c.outcome_source, lo, hi, None)
        conditioned = False
        notes: list[str] = []
        if c.condition_on_regime:
            reg = [s for s in samples if s.regime == regime.value]
            if len(reg) >= c.min_samples_regime:
                samples, conditioned = reg, True
            else:
                notes.append(
                    f"regime {regime.value}: {len(reg)} samples < {c.min_samples_regime}, pooled across regimes"
                )
        n = len(samples)
        est = EVEstimate(
            status="INSUFFICIENT_DATA",
            n=n,
            cost_frac=roundtrip_cost_frac,
            other_cost_frac=other_cost_frac,
            execution_risk_frac=execution_risk_frac,
            bucket=f"[{lo:g},{hi:g})",
            conditioned_on_regime=conditioned,
            notes=notes,
        )
        if n < c.min_samples:
            est.notes.append(f"INSUFFICIENT DATA: {n} comparable outcomes < {c.min_samples}")
            return est
        nets = [s.gross_return - roundtrip_cost_frac for s in samples]
        wins = [x for x in nets if x > 0]
        losses = [-x for x in nets if x <= 0]
        p = len(wins) / n
        p_lo = wilson_lower(len(wins), n, c.confidence_z)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        extra = other_cost_frac + execution_risk_frac
        est.status = "OK"
        est.p_win, est.p_win_lower, est.avg_win, est.avg_loss = p, p_lo, avg_win, avg_loss
        est.ev = p * avg_win - (1 - p) * avg_loss - extra
        est.ev_conservative = p_lo * avg_win - (1 - p_lo) * avg_loss - extra
        return est
