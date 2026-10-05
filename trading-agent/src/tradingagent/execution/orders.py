"""Order intents and execution reports — the only objects that cross the strategy/execution boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from tradingagent.common.types import ExecStatus, Side, Venue


def new_execution_id() -> str:
    return "ex_" + uuid.uuid4().hex


@dataclass
class OrderIntent:
    execution_id: str
    mint: str
    side: Side
    venue: Venue
    amount_in: int  # lamports (buy) / token base units (sell)
    min_out: int
    expected_out: int
    expected_price: float  # all-in lamports per base unit
    spot_price: float
    expected_slippage: float
    priority_micro_lamports: int
    reason: str
    created_at: float
    decision_id: str | None = None
    position_id: str | None = None
    token_program: str | None = None
    creator: str | None = None
    pool: str | None = None
    is_exit: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionReport:
    execution_id: str
    status: ExecStatus
    mint: str
    side: Side
    venue: Venue
    requested_amount: int
    actual_in: int = 0
    actual_out: int = 0
    expected_out: int = 0
    expected_price: float = 0.0
    actual_price: float = 0.0
    slippage_vs_expected: float = 0.0  # positive = worse than expected
    platform_fees: int = 0
    network_fees: int = 0
    priority_fee: int = 0
    signature: str | None = None
    blockhash: str | None = None
    last_valid_block_height: int | None = None
    slot: int | None = None
    submitted_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    spot_at_fill: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        return self.status in (ExecStatus.CONFIRMED, ExecStatus.FILLED_PAPER, ExecStatus.FILLED_SHADOW)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        d["side"] = self.side.value
        d["venue"] = self.venue.value
        return d
