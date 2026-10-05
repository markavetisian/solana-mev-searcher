"""Time abstraction.

Every component asks a Clock for "now". Live code uses SystemClock; the backtester drives a ManualClock that
only moves forward as historical events are replayed, which is what makes look-ahead structurally impossible:
a component cannot see an event that has not been replayed yet, and the clock cannot be moved backwards.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds since the Unix epoch."""
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class ClockWentBackwards(RuntimeError):
    pass


class ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance_to(self, t: float) -> None:
        if t < self._now:
            raise ClockWentBackwards(f"refusing to move clock backwards: {t} < {self._now}")
        self._now = t

    def advance(self, dt: float) -> None:
        self.advance_to(self._now + dt)
