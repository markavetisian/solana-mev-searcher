"""Minimal in-process metrics registry (counters, gauges, latency histograms) with Prometheus text export.

Deliberately dependency-free: the worker exports `snapshot()` into the runtime-state store so the API process
can serve it, and `render_prometheus()` for scraping.
"""

from __future__ import annotations

import bisect
import threading
import time
from collections import deque
from dataclasses import dataclass, field

_LATENCY_BUCKETS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)


@dataclass
class _Histogram:
    buckets: tuple[float, ...] = _LATENCY_BUCKETS_MS
    counts: list[int] = field(default_factory=lambda: [0] * (len(_LATENCY_BUCKETS_MS) + 1))
    total: float = 0.0
    n: int = 0
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=512))

    def observe(self, v: float) -> None:
        self.counts[bisect.bisect_left(self.buckets, v)] += 1
        self.total += v
        self.n += 1
        self.recent.append(v)

    def quantile(self, q: float) -> float | None:
        if not self.recent:
            return None
        s = sorted(self.recent)
        return s[min(len(s) - 1, int(q * len(s)))]


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = {}
        self.gauges: dict[str, float] = {}
        self.histograms: dict[str, _Histogram] = {}
        self._rates: dict[str, deque[float]] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value
            self._rates.setdefault(name, deque(maxlen=10_000)).append(time.monotonic())

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = value

    def observe_ms(self, name: str, value_ms: float) -> None:
        with self._lock:
            self.histograms.setdefault(name, _Histogram()).observe(value_ms)

    def rate_per_sec(self, name: str, window_s: float = 60.0) -> float:
        with self._lock:
            q = self._rates.get(name)
            if not q:
                return 0.0
            cutoff = time.monotonic() - window_s
            n = sum(1 for t in q if t >= cutoff)
            return n / window_s

    def snapshot(self) -> dict:
        with self._lock:
            hist = {
                k: {
                    "count": h.n,
                    "mean_ms": (h.total / h.n) if h.n else None,
                    "p50_ms": h.quantile(0.5),
                    "p95_ms": h.quantile(0.95),
                    "p99_ms": h.quantile(0.99),
                }
                for k, h in self.histograms.items()
            }
            snap = {"counters": dict(self.counters), "gauges": dict(self.gauges), "latency": hist}
        snap["rates_per_sec_60s"] = {k: self.rate_per_sec(k) for k in list(self._rates)}
        return snap

    def render_prometheus(self, prefix: str = "ta_") -> str:
        lines: list[str] = []
        with self._lock:
            for k, v in sorted(self.counters.items()):
                n = prefix + _clean(k) + "_total"
                lines += [f"# TYPE {n} counter", f"{n} {v}"]
            for k, v in sorted(self.gauges.items()):
                n = prefix + _clean(k)
                lines += [f"# TYPE {n} gauge", f"{n} {v}"]
            for k, h in sorted(self.histograms.items()):
                n = prefix + _clean(k) + "_ms"
                lines.append(f"# TYPE {n} histogram")
                cum = 0
                for b, c in zip(h.buckets, h.counts, strict=False):
                    cum += c
                    lines.append(f'{n}_bucket{{le="{b}"}} {cum}')
                lines.append(f'{n}_bucket{{le="+Inf"}} {h.n}')
                lines.append(f"{n}_sum {h.total}")
                lines.append(f"{n}_count {h.n}")
        return "\n".join(lines) + "\n"


def _clean(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


class Timer:
    """with Timer(metrics, "feature_calc"): ..."""

    def __init__(self, metrics: Metrics, name: str) -> None:
        self.metrics, self.name = metrics, name
        self.elapsed_ms = 0.0

    def __enter__(self) -> Timer:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._t0) * 1000
        self.metrics.observe_ms(self.name, self.elapsed_ms)


METRICS = Metrics()
