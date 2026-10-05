"""Transparent 0-100 score.

Each group (momentum, volume, ...) is a weighted average of feature maps; each map is a clipped linear transform
of one raw feature onto 0..100 (`lo` -> 0, `hi` -> 100; inverted if lo > hi). Missing features score
`missing_feature_score` (default 0: unknown is not neutral). The total is the weight-sum of groups.
Everything is in config; nothing is fitted here. Weights are defaults to be evaluated out-of-sample, not truths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradingagent.common.config import ScoringConfig
from tradingagent.common.versions import SCORING_VERSION
from tradingagent.features.engine import FeatureVector


def map_linear(x: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 100.0 if x >= hi else 0.0
    z = (x - lo) / (hi - lo)
    return 100.0 * min(1.0, max(0.0, z))


@dataclass
class ComponentScore:
    name: str
    score: float
    weight: float
    inputs: dict[str, float | None] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


@dataclass
class ScoreCard:
    mint: str
    as_of: float
    total: float
    components: dict[str, ComponentScore]
    version: str = SCORING_VERSION

    def explain(self) -> str:
        lines = [f"TOKEN SCORE: {self.total:.0f}", ""]
        for c in sorted(self.components.values(), key=lambda c: -c.weight):
            miss = f"  (missing: {', '.join(c.missing)})" if c.missing else ""
            lines.append(f"{c.name.replace('_', ' ').title():<16}{c.score:>5.0f}  w={c.weight:.2f}{miss}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 2),
            "version": self.version,
            "components": {
                k: {"score": round(c.score, 2), "weight": c.weight, "inputs": c.inputs, "missing": c.missing}
                for k, c in self.components.items()
            },
        }


class Scorer:
    def __init__(self, cfg: ScoringConfig) -> None:
        self.cfg = cfg

    def score(self, fv: FeatureVector) -> ScoreCard:
        comps: dict[str, ComponentScore] = {}
        total = 0.0
        for group, weight in self.cfg.weights.items():
            maps = self.cfg.components[group]
            wsum = sum(m.weight for m in maps) or 1.0
            acc = 0.0
            inputs: dict[str, float | None] = {}
            missing: list[str] = []
            for m in maps:
                v = fv.get(m.feature)
                inputs[m.feature] = v
                if v is None:
                    missing.append(m.feature)
                    acc += m.weight * self.cfg.missing_feature_score
                else:
                    acc += m.weight * map_linear(float(v), m.lo, m.hi)
            gs = acc / wsum
            comps[group] = ComponentScore(group, gs, weight, inputs, missing)
            total += weight * gs
        return ScoreCard(mint=fv.mint, as_of=fv.as_of, total=total, components=comps)
