"""Append-only, time-indexed trade series with look-ahead-safe window queries.

`window(as_of, seconds)` returns only rows with t <= as_of. The as_of bound is mandatory: there is no API that
returns "everything", so feature code cannot accidentally read rows from the future during a replay.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass


class LookAheadError(RuntimeError):
    pass


@dataclass(slots=True)
class Trade:
    t: float
    slot: int
    side: int  # +1 buy, -1 sell
    sol: int  # lamports through the curve/pool, excl. fees
    tokens: int
    user: str
    price: float  # spot after trade, lamports per base unit
    liquidity: int  # quote reserves after trade (lamports)
    venue: str


class TradeSeries:
    __slots__ = ("_cbuy", "_csell", "_rows", "_t", "last_t", "max_age_s", "total_count")

    def __init__(self, max_age_s: float = 4 * 3600.0) -> None:
        self._t: list[float] = []
        self._rows: list[Trade] = []
        self._cbuy: list[int] = []  # cumulative buy lamports (inclusive)
        self._csell: list[int] = []
        self.max_age_s = max_age_s
        self.last_t = float("-inf")
        self.total_count = 0

    def __len__(self) -> int:
        return len(self._rows)

    def append(self, tr: Trade) -> None:
        if tr.t < self.last_t:
            # Events of one token are processed in arrival order; tolerate tiny clock skew by clamping.
            tr.t = self.last_t
        self._t.append(tr.t)
        self._rows.append(tr)
        pb = self._cbuy[-1] if self._cbuy else 0
        ps = self._csell[-1] if self._csell else 0
        self._cbuy.append(pb + (tr.sol if tr.side > 0 else 0))
        self._csell.append(ps + (tr.sol if tr.side < 0 else 0))
        self.last_t = tr.t
        self.total_count += 1
        if len(self._t) > 512 and self._t[0] < tr.t - self.max_age_s:
            cut = bisect.bisect_left(self._t, tr.t - self.max_age_s)
            del self._t[:cut]
            del self._rows[:cut]
            del self._cbuy[:cut]
            del self._csell[:cut]

    def window(self, as_of: float, seconds: float, now: float | None = None) -> list[Trade]:
        if now is not None and as_of > now + 1e-9:
            raise LookAheadError(f"as_of {as_of} is after now {now}")
        hi = bisect.bisect_right(self._t, as_of)
        lo = bisect.bisect_left(self._t, as_of - seconds) if seconds != float("inf") else 0
        # strictly-after lower bound: (as_of - seconds, as_of]
        while lo < hi and self._t[lo] <= as_of - seconds:
            lo += 1
        return self._rows[lo:hi]

    def _bounds(self, start: float, end: float) -> tuple[int, int]:
        return bisect.bisect_right(self._t, start), bisect.bisect_right(self._t, end)

    def volume_between(self, start: float, end: float) -> tuple[int, int, int]:
        """(buy lamports, sell lamports, trade count) for start < t <= end, in O(log n)."""
        lo, hi = self._bounds(start, end)
        if hi <= lo:
            return 0, 0, 0
        b = self._cbuy[hi - 1] - (self._cbuy[lo - 1] if lo else 0)
        s_ = self._csell[hi - 1] - (self._csell[lo - 1] if lo else 0)
        return b, s_, hi - lo

    def between(self, start: float, end: float) -> list[Trade]:
        """Rows with start < t <= end."""
        lo = bisect.bisect_right(self._t, start)
        hi = bisect.bisect_right(self._t, end)
        return self._rows[lo:hi]

    def last_before(self, as_of: float) -> Trade | None:
        i = bisect.bisect_right(self._t, as_of)
        return self._rows[i - 1] if i else None

    def first(self) -> Trade | None:
        return self._rows[0] if self._rows else None

    def price_at(self, as_of: float) -> float | None:
        tr = self.last_before(as_of)
        return tr.price if tr else None
