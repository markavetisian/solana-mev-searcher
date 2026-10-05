"""Pump fee schedule (market-cap tiered), mirroring pump-fees-math::calculate_fee_tier and the SDKs.

Bonding curve trades pay protocol + creator bps (creator part only when the curve has a creator set).
PumpSwap canonical pools pay lp + protocol + coin-creator bps from the same tiers; non-canonical pools pay the
flat schedule. Fee amounts are ceil(amount * bps / 10_000), as in the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Fees:
    lp_bps: int
    protocol_bps: int
    creator_bps: int

    @property
    def curve_total_bps(self) -> int:
        return self.protocol_bps + self.creator_bps

    @property
    def pool_total_bps(self) -> int:
        return self.lp_bps + self.protocol_bps + self.creator_bps


@dataclass(frozen=True, slots=True)
class FeeTier:
    market_cap_lamports_threshold: int
    fees: Fees


@dataclass(frozen=True)
class FeeSchedule:
    tiers: tuple[FeeTier, ...]
    flat: Fees

    @classmethod
    def from_rows(cls, rows: list[tuple[int, int, int, int]], flat: tuple[int, int, int]) -> FeeSchedule:
        tiers = tuple(FeeTier(r[0], Fees(r[1], r[2], r[3])) for r in rows)
        if not tiers:
            raise ValueError("fee tiers cannot be empty")
        return cls(tiers=tiers, flat=Fees(*flat))

    @classmethod
    def from_onchain(cls, fee_config: dict) -> FeeSchedule:
        """Build from a decoded FeeConfig account (pump_fees / pump IDL layout)."""
        tiers = tuple(
            FeeTier(
                int(t["market_cap_lamports_threshold"]),
                Fees(
                    int(t["fees"]["lp_fee_bps"]), int(t["fees"]["protocol_fee_bps"]), int(t["fees"]["creator_fee_bps"])
                ),
            )
            for t in fee_config["fee_tiers"]
        )
        f = fee_config["flat_fees"]
        return cls(tiers=tiers, flat=Fees(int(f["lp_fee_bps"]), int(f["protocol_fee_bps"]), int(f["creator_fee_bps"])))

    def tier_for(self, market_cap_lamports: int) -> Fees:
        first = self.tiers[0]
        if market_cap_lamports < first.market_cap_lamports_threshold:
            return first.fees
        for tier in reversed(self.tiers):
            if market_cap_lamports >= tier.market_cap_lamports_threshold:
                return tier.fees
        return first.fees


def ceil_div(a: int, b: int) -> int:
    if b <= 0:
        raise ZeroDivisionError("ceil_div by non-positive")
    return (a + b - 1) // b


def fee_amount(amount: int, bps: int) -> int:
    return ceil_div(amount * bps, 10_000)
