"""Global kill switch and soft pause.

PAUSE  — operator action: stop new entries, keep managing open positions. Resume any time.
KILL   — stop ALL strategy-generated orders (entries and exits), preserve state, alert, optionally run the configured
         emergency exit, and LATCH: only an explicit operator reset (with a confirmation phrase) clears it, and the
         latched state is persisted so a process restart does not clear it.

Automatic triggers: daily loss limit, max drawdown, repeated execution failures, abnormal slippage, stale market
data, RPC degradation, database errors, portfolio/wallet mismatch, uncertain transactions.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from tradingagent.common.config import KillSwitchConfig
from tradingagent.common.logging import get_logger
from tradingagent.common.metrics import METRICS

log = get_logger("risk.killswitch")
RESET_PHRASE = "RESET_KILL_SWITCH"


@dataclass
class KillState:
    killed: bool = False
    paused: bool = False
    reason: str | None = None
    source: str | None = None
    activated_at: float | None = None
    pause_reason: str | None = None
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "killed": self.killed,
            "paused": self.paused,
            "reason": self.reason,
            "source": self.source,
            "activated_at": self.activated_at,
            "pause_reason": self.pause_reason,
            "history": self.history[-20:],
        }


class KillSwitch:
    def __init__(
        self,
        cfg: KillSwitchConfig,
        clock_now: Callable[[], float] = time.time,
        on_change: Callable[[KillState, str], None] | None = None,
    ) -> None:
        self.cfg, self.now = cfg, clock_now
        self.state = KillState()
        self.on_change = on_change
        self._exec_failures: deque[float] = deque()
        self._slippage_events: deque[float] = deque()
        self._stale_since: float | None = None
        self._rpc_bad_since: float | None = None
        self._db_errors = 0

    # ---- operator controls --------------------------------------------------------------------------------
    @property
    def entries_allowed(self) -> bool:
        return not (self.state.killed or self.state.paused)

    @property
    def orders_allowed(self) -> bool:
        return not self.state.killed

    def activate(self, reason: str, source: str = "auto") -> None:
        if self.state.killed:
            return
        self.state.killed, self.state.reason, self.state.source = True, reason, source
        self.state.activated_at = self.now()
        self.state.history.append({"t": self.now(), "action": "KILL", "reason": reason, "source": source})
        METRICS.inc("killswitch.activated")
        log.critical("kill_switch_activated", reason=reason, source=source)
        if self.on_change:
            self.on_change(self.state, "KILL")

    def reset(self, operator: str, confirm: str) -> bool:
        if confirm != RESET_PHRASE:
            return False
        self.state.history.append(
            {"t": self.now(), "action": "RESET", "operator": operator, "previous_reason": self.state.reason}
        )
        self.state.killed, self.state.reason, self.state.source = False, None, None
        self._exec_failures.clear()
        self._slippage_events.clear()
        self._db_errors = 0
        log.warning("kill_switch_reset", operator=operator)
        if self.on_change:
            self.on_change(self.state, "RESET")
        return True

    def pause(self, reason: str, operator: str = "operator") -> None:
        self.state.paused, self.state.pause_reason = True, reason
        self.state.history.append({"t": self.now(), "action": "PAUSE", "reason": reason, "operator": operator})
        if self.on_change:
            self.on_change(self.state, "PAUSE")

    def resume(self, operator: str = "operator") -> None:
        self.state.paused, self.state.pause_reason = False, None
        self.state.history.append({"t": self.now(), "action": "RESUME", "operator": operator})
        if self.on_change:
            self.on_change(self.state, "RESUME")

    def restore(self, saved: dict) -> None:
        """Re-apply a persisted latched state on startup."""
        if saved.get("killed"):
            self.state.killed = True
            self.state.reason = saved.get("reason") or "restored from persisted state"
            self.state.source = saved.get("source") or "persisted"
            self.state.activated_at = saved.get("activated_at")
        if saved.get("paused"):
            self.state.paused, self.state.pause_reason = True, saved.get("pause_reason")
        self.state.history = list(saved.get("history") or [])

    # ---- automatic triggers -------------------------------------------------------------------------------
    def _window(self, q: deque[float], window_s: float) -> int:
        now = self.now()
        q.append(now)
        while q and q[0] < now - window_s:
            q.popleft()
        return len(q)

    def record_execution_failure(self, detail: str) -> None:
        n = self._window(self._exec_failures, self.cfg.execution_failure_window_s)
        if n >= self.cfg.max_execution_failures:
            self.activate(f"{n} execution failures in {self.cfg.execution_failure_window_s:.0f}s (last: {detail})")

    def record_slippage(self, actual: float, expected: float) -> None:
        if actual > max(expected, 0.005) * self.cfg.abnormal_slippage_multiple and actual > 0.01:
            n = self._window(self._slippage_events, self.cfg.execution_failure_window_s)
            log.warning("abnormal_slippage", actual=actual, expected=expected)
            if n >= self.cfg.max_abnormal_slippage_events:
                self.activate(f"abnormal slippage {n} times (last {actual:.2%} vs expected {expected:.2%})")

    def observe_staleness(self, staleness_s: float, has_open_positions: bool = True) -> None:
        if staleness_s > self.cfg.stale_data_kill_after_s / 4:
            if self._stale_since is None:
                self._stale_since = self.now()
            if self.now() - self._stale_since > self.cfg.stale_data_kill_after_s and has_open_positions:
                self.activate(f"market data stale for {self.now() - self._stale_since:.0f}s with open positions")
        else:
            self._stale_since = None

    def observe_rpc(self, healthy: bool) -> None:
        if not healthy:
            if self._rpc_bad_since is None:
                self._rpc_bad_since = self.now()
            if self.now() - self._rpc_bad_since > self.cfg.rpc_degraded_kill_after_s:
                self.activate(f"RPC degraded for {self.now() - self._rpc_bad_since:.0f}s")
        else:
            self._rpc_bad_since = None

    def record_db_error(self, detail: str) -> None:
        self._db_errors += 1
        if self._db_errors >= self.cfg.db_error_kill_after:
            self.activate(f"database errors ({self._db_errors}); failing closed: {detail}")

    def record_db_ok(self) -> None:
        self._db_errors = 0

    def uncertain_transaction(self, execution_id: str) -> None:
        self.activate(f"transaction {execution_id} status UNCERTAIN; reconcile before trading")

    def mismatch(self, what: str) -> None:
        self.activate(what)
