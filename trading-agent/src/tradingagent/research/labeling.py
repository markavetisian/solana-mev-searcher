"""Outcome labeling: what actually happened after each candidate signal.

For every evaluated candidate (passed or rejected) we freeze the feature vector at signal time and then, as the
clock moves forward, record at each horizon (default 30s, 1m, 5m, 15m):
  * spot return
  * EXECUTABLE round-trip return: buy `label_notional_sol` at signal time on the exact venue state, sell the
    resulting tokens at the horizon state — fees and both legs' price impact included
  * max favourable / adverse excursion of spot over the horizon
  * whether the token died / graduated within the horizon
A label is only finalised once the clock has passed the horizon, so no label can leak into a decision made before it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tradingagent.common.types import LifecycleState
from tradingagent.common.units import LAMPORTS_PER_SOL
from tradingagent.market.engine import MarketDataEngine
from tradingagent.pump.adapters import QuoteError
from tradingagent.strategy.decision import Decision


@dataclass
class PendingLabel:
    decision_id: str
    mint: str
    t: float
    entry_spot: float
    tokens: int  # bought with the notional at signal time
    spend: int
    venue: str
    score: float | None
    regime: str | None
    features: dict[str, Any]
    passed_stage1: bool
    outcome: str
    labels: dict[str, Any] = field(default_factory=dict)
    done_horizons: set[int] = field(default_factory=set)


class OutcomeLabeler:
    def __init__(
        self,
        market: MarketDataEngine,
        horizons: list[int],
        notional_sol: float,
        feature_version: str,
        max_pending: int = 50_000,
    ) -> None:
        self.market, self.horizons, self.notional = market, sorted(horizons), notional_sol
        self.feature_version = feature_version
        self.pending: dict[str, PendingLabel] = {}
        self.completed: list[dict] = []
        self.max_pending = max_pending
        self.protected_mints: set[str] = set()

    def on_decision(self, d: Decision) -> None:
        if d.features is None or len(self.pending) >= self.max_pending:
            return
        tm = self.market.market(d.mint)
        adapter = tm.adapter(self.market.schedule) if tm else None
        if adapter is None:
            return
        spend = int(self.notional * LAMPORTS_PER_SOL)
        try:
            q = adapter.quote_buy(spend)
        except QuoteError:
            return
        self.pending[d.decision_id] = PendingLabel(
            decision_id=d.decision_id,
            mint=d.mint,
            t=d.t,
            entry_spot=q.spot_price_before,
            tokens=q.amount_out,
            spend=q.amount_in,
            venue=adapter.venue.value,
            score=d.score.total if d.score else None,
            regime=d.regime.regime.value if d.regime else None,
            features=dict(d.features.values),
            passed_stage1=d.stage1_passed,
            outcome=d.outcome.value,
        )
        self.protected_mints.add(d.mint)

    def step(self, now: float) -> list[dict]:
        finished: list[dict] = []
        for did, p in list(self.pending.items()):
            for h in self.horizons:
                if h in p.done_horizons or now < p.t + h:
                    continue
                self._label(p, h, p.t + h)
                p.done_horizons.add(h)
            if len(p.done_horizons) == len(self.horizons):
                row = self._row(p)
                finished.append(row)
                self.completed.append(row)
                del self.pending[did]
        self.protected_mints = {p.mint for p in self.pending.values()}
        return finished

    def _label(self, p: PendingLabel, h: int, at: float) -> None:
        tm = self.market.market(p.mint)
        key = f"{h}s"
        if tm is None:
            p.labels.update({f"spot_ret_{key}": -1.0, f"exec_ret_{key}": -1.0, f"dead_{key}": True})
            return
        rows = tm.series.between(p.t, at)
        last = tm.series.last_before(at)
        px = last.price if last else p.entry_spot
        if rows:
            hi = max(r.price for r in rows)
            lo = min(r.price for r in rows)
        else:
            hi = lo = px
        p.labels[f"spot_ret_{key}"] = px / p.entry_spot - 1 if p.entry_spot else None
        p.labels[f"mfe_{key}"] = hi / p.entry_spot - 1 if p.entry_spot else None
        p.labels[f"mae_{key}"] = lo / p.entry_spot - 1 if p.entry_spot else None
        st = tm.record.state
        p.labels[f"dead_{key}"] = st in (LifecycleState.DEAD, LifecycleState.UNTRADEABLE)
        p.labels[f"graduated_{key}"] = tm.record.graduation_state != "NONE"
        adapter = tm.adapter(self.market.schedule)
        exec_ret = None
        if adapter is not None:
            try:
                exec_ret = adapter.quote_sell(p.tokens).amount_out / p.spend - 1
            except QuoteError:
                exec_ret = -1.0
        elif st is LifecycleState.GRADUATING:
            exec_ret = None  # no venue yet; unknown, not zero
        else:
            exec_ret = -1.0
        p.labels[f"exec_ret_{key}"] = exec_ret

    def _row(self, p: PendingLabel) -> dict:
        return {
            "decision_id": p.decision_id,
            "mint": p.mint,
            "t": p.t,
            "score": p.score,
            "regime": p.regime,
            "venue": p.venue,
            "passed_stage1": p.passed_stage1,
            "outcome": p.outcome,
            "feature_version": self.feature_version,
            "features": p.features,
            "labels": p.labels,
        }
