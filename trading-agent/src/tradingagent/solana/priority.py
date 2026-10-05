"""Priority fee policy: static or a percentile of recent prioritization fees on the touched accounts, clamped."""

from __future__ import annotations

import time

from tradingagent.common.config import PriorityFeePolicy
from tradingagent.solana.rpc import RpcError, RpcPool


class PriorityFeeOracle:
    def __init__(self, policy: PriorityFeePolicy, rpc: RpcPool | None = None, cache_s: float = 5.0) -> None:
        self.policy, self.rpc, self.cache_s = policy, rpc, cache_s
        self._cache: tuple[float, int] | None = None

    def clamp(self, v: int) -> int:
        return max(self.policy.min_micro_lamports, min(self.policy.max_micro_lamports, v))

    def current(self) -> int:
        if self.policy.mode == "static" or self._cache is None:
            return self.clamp(self.policy.static_micro_lamports)
        return self._cache[1]

    async def refresh(self, accounts: list[str]) -> int:
        if self.policy.mode == "static" or self.rpc is None:
            return self.current()
        if self._cache and time.time() - self._cache[0] < self.cache_s:
            return self._cache[1]
        try:
            fees = sorted(await self.rpc.get_recent_prioritization_fees(accounts))
        except RpcError:
            return self.current()
        if not fees:
            return self.current()
        v = fees[min(len(fees) - 1, int(len(fees) * self.policy.percentile / 100))]
        self._cache = (time.time(), self.clamp(v))
        return self._cache[1]
