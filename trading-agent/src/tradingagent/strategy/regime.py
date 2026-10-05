"""Market-regime detection from market-wide activity across all tracked tokens.

Inputs (all as-of now): trades/min, launches/min, buy ratio, cross-sectional distribution of 5-minute returns of
active tokens, data staleness and RPC health. Priority: DEGRADED > LOW_ACTIVITY > EXTREME_VOLATILITY > RISK_OFF >
HIGH_MOMENTUM > RISK_ON > NORMAL. Regime only ever tightens or loosens *within* configured overrides.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from tradingagent.common.config import RegimeConfig, RegimeOverride
from tradingagent.common.types import LifecycleState, Regime
from tradingagent.market.engine import MarketDataEngine


@dataclass
class RegimeState:
    regime: Regime
    stats: dict
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"regime": self.regime.value, "stats": self.stats, "reasons": self.reasons}


class RegimeDetector:
    def __init__(self, cfg: RegimeConfig) -> None:
        self.cfg = cfg
        self.current = RegimeState(Regime.NORMAL, {}, [])

    def override(self, regime: Regime) -> RegimeOverride:
        return self.cfg.overrides.get(regime.value, RegimeOverride())

    def evaluate(
        self, engine: MarketDataEngine, now: float, rpc_healthy: bool = True, feed_connected: bool = True
    ) -> RegimeState:
        c = self.cfg
        st = engine.activity.stats(now)
        rets: list[float] = []
        for tm in engine.tokens.values():
            if tm.record.state not in (LifecycleState.BONDING_CURVE, LifecycleState.PUMPSWAP, LifecycleState.ACTIVE):
                continue
            if now - (tm.record.last_trade_at or 0) > c.window_s:
                continue
            ref = tm.series.last_before(now - c.window_s)
            px = tm.spot_price()
            if ref and px and ref.price > 0:
                rets.append(px / ref.price - 1)
        st["active_tokens"] = len(rets)
        st["median_ret_window"] = statistics.median(rets) if rets else None
        st["dispersion_window"] = (
            statistics.quantiles(rets, n=4)[2] - statistics.quantiles(rets, n=4)[0] if len(rets) >= 8 else None
        )
        st["up_fraction"] = (sum(1 for r in rets if r > 0) / len(rets)) if rets else None
        reasons: list[str] = []
        if not rpc_healthy or not feed_connected or st["data_staleness_s"] > c.degraded_staleness_s:
            reasons.append(
                f"data/execution degraded (staleness={st['data_staleness_s']:.1f}s, rpc={rpc_healthy}, "
                f"feed={feed_connected})"
            )
            regime = Regime.DEGRADED
        elif st["trades_per_min"] < c.low_activity_trades_per_min:
            reasons.append(f"trades/min {st['trades_per_min']:.0f} < {c.low_activity_trades_per_min:.0f}")
            regime = Regime.LOW_ACTIVITY
        elif st["dispersion_window"] is not None and st["dispersion_window"] > c.extreme_volatility_dispersion:
            reasons.append(f"cross-sectional IQR of returns {st['dispersion_window']:.2f}")
            regime = Regime.EXTREME_VOLATILITY
        elif st["median_ret_window"] is not None and st["median_ret_window"] < c.risk_off_median_ret:
            reasons.append(f"median token return {st['median_ret_window']:.1%}")
            regime = Regime.RISK_OFF
        elif st["median_ret_window"] is not None and st["median_ret_window"] > c.high_momentum_median_ret:
            reasons.append(f"median token return {st['median_ret_window']:.1%}")
            regime = Regime.HIGH_MOMENTUM
        elif st["up_fraction"] is not None and st["up_fraction"] > c.risk_on_up_fraction:
            reasons.append(f"{st['up_fraction']:.0%} of active tokens up")
            regime = Regime.RISK_ON
        else:
            regime = Regime.NORMAL
        self.current = RegimeState(regime, st, reasons)
        return self.current
