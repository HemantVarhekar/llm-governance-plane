"""Observability — the audit trail and the metrics.

Both are telemetry about the same requests, so they live together.

EventSink writes ONE JSON line per request: the audit trail AND the cost
ledger. Same schema goes to Kafka at checkpoint 4.

Metrics exposes Prometheus text format so a load test has a server-side view.
Deliberately dependency-free: the exposition format is simple enough to emit
by hand, and we are not spending 20MB of RAM on a client library.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Audit / event sink
# --------------------------------------------------------------------------


class EventSink:
    """Append-only JSONL.

    NOTE: writes synchronously. A small append is microseconds, but it does
    briefly block the event loop. Left deliberately simple so the load test
    can tell us whether it actually matters, rather than pre-optimising
    something we have not measured. Becomes an async Kafka producer at
    checkpoint 4; the schema does not change.
    """

    def __init__(self, path: str | Path = "events.jsonl") -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def emit(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def all_records(self) -> Iterable[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

# Buckets chosen for THIS hardware: a local call is 6-10s, so sub-second
# buckets would all be empty and 30s+ is where the chain deadline bites.
LATENCY_BUCKETS = (0.5, 1.0, 2.5, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0, 40.0)

# Event-loop lag sits three orders of magnitude below request latency.
# Reusing the ladder above would drop every healthy sample into the first
# bucket and leave the histogram blind exactly where it has to see.
LOOP_LAG_BUCKETS = (
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
)

DEFAULT_HISTOGRAM = "mg_request_duration_seconds"

# A histogram's buckets are part of its contract: once a dashboard or an
# alert reads le="10", changing the ladder silently changes what that
# query means. Declaring them in one place keeps that decision visible.
HISTOGRAM_BUCKETS: dict[str, tuple[float, ...]] = {
    DEFAULT_HISTOGRAM: LATENCY_BUCKETS,
    "mg_event_loop_lag_seconds": LOOP_LAG_BUCKETS,
}


def _labels(pairs: dict[str, str]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(pairs.items()))
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _quantile(buckets: list[int], edges: tuple[float, ...],
              q: float) -> float | None:
    """Estimate a quantile from bucket counts, the way Prometheus does.

    A histogram does not store samples, so this is an ESTIMATE bounded by
    the ladder: it interpolates linearly inside the bucket the quantile
    falls in. Two honest limits come with that. Resolution can never beat
    the bucket width, and a quantile that lands in the +Inf bucket cannot
    be bounded above at all - the last edge is returned, and the true
    value is somewhere above it.

    Worth having anyway: p99 from buckets is the number an SLO is written
    against, and computing it here means the dashboard and /healthz agree
    with what Prometheus would say rather than inventing their own.
    """
    total = sum(buckets)
    if total <= 0:
        return None
    target = q * total
    cumulative = 0
    lower = 0.0
    for i, edge in enumerate(edges):
        cumulative += buckets[i]
        if cumulative >= target:
            in_bucket = buckets[i]
            if in_bucket <= 0:
                return round(float(edge), 6)
            previous = cumulative - in_bucket
            fraction = (target - previous) / in_bucket
            return round(lower + fraction * (float(edge) - lower), 6)
        lower = float(edge)
    # Fell into +Inf: report the last edge as a floor, not a value.
    return round(float(edges[-1]), 6)


class Metrics:
    """Minimal Prometheus-compatible counters, gauges and one histogram."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple], float] = defaultdict(float)
        # Histograms are keyed by (metric name, label tuple), so more than
        # one histogram can live in the same registry with its own ladder.
        self._hist_buckets: dict[tuple[str, tuple], list[int]] = {}
        self._hist_sum: dict[tuple[str, tuple], float] = defaultdict(float)
        self._hist_count: dict[tuple[str, tuple], int] = defaultdict(int)
        self.started = time.time()

    # -- recording ---------------------------------------------------------

    def inc(self, name: str, labels: dict[str, str] | None = None, by: float = 1.0):
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._counters[key] += by

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None):
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._gauges[key] = value

    def set_counter(self, name: str, value: float,
                    labels: dict[str, str] | None = None):
        """Set a counter to an absolute value.

        Needed because some counters are not ours to increment: the CPU
        seconds and bytes-written a process has accumulated are read from
        the operating system already cumulative. Incrementing by a delta we
        computed would lose a sample's worth of work on every restart of
        the sampler and drift from the true total.

        A counter must never go backwards - Prometheus reads any decrease
        as a process restart and resets its rate calculation - so a lower
        value is refused rather than written.
        """
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            if value >= self._counters.get(key, 0.0):
                self._counters[key] = value

    def observe(self, seconds: float, labels: dict[str, str] | None = None,
                name: str = DEFAULT_HISTOGRAM):
        edges = HISTOGRAM_BUCKETS.get(name, LATENCY_BUCKETS)
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            buckets = self._hist_buckets.setdefault(key, [0] * (len(edges) + 1))
            placed = False
            for i, edge in enumerate(edges):
                if seconds <= edge:
                    buckets[i] += 1
                    placed = True
                    break
            if not placed:
                buckets[-1] += 1
            self._hist_sum[key] += seconds
            self._hist_count[key] += 1

    # -- reading -----------------------------------------------------------

    def get_gauge(self, name: str,
                  labels: dict[str, str] | None = None) -> float | None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            return self._gauges.get(key)

    def get_counter(self, name: str,
                    labels: dict[str, str] | None = None) -> float | None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            return self._counters.get(key)

    def histogram(self, name: str,
                  labels: dict[str, str] | None = None) -> dict[str, Any] | None:
        """Count, mean and estimated quantiles for one histogram series."""
        key = (name, tuple(sorted((labels or {}).items())))
        edges = HISTOGRAM_BUCKETS.get(name, LATENCY_BUCKETS)
        with self._lock:
            count = self._hist_count.get(key, 0)
            if not count:
                return None
            buckets = list(self._hist_buckets.get(key, []))
            total = self._hist_sum.get(key, 0.0)
        return {
            "count": count,
            "sum_s": round(total, 6),
            "avg_s": round(total / count, 6),
            "p50_s": _quantile(buckets, edges, 0.50),
            "p95_s": _quantile(buckets, edges, 0.95),
            "p99_s": _quantile(buckets, edges, 0.99),
        }

    def snapshot(self) -> dict[str, Any]:
        """Plain JSON view, for /healthz and for humans."""
        with self._lock:
            counters = {
                f"{n}{_labels(dict(l))}": v for (n, l), v in self._counters.items()
            }
            gauges = {
                f"{n}{_labels(dict(l))}": v for (n, l), v in self._gauges.items()
            }
            hist: dict[str, dict[str, Any]] = {}
            for (name, lbl) in self._hist_count:
                key = (name, lbl)
                count = self._hist_count[key]
                total = self._hist_sum[key]
                edges = HISTOGRAM_BUCKETS.get(name, LATENCY_BUCKETS)
                entry = {
                    "count": count,
                    "sum_s": round(total, 4),
                    "avg_s": round(total / count, 4) if count else 0,
                    "p50_s": _quantile(self._hist_buckets[key], edges, 0.50),
                    "p95_s": _quantile(self._hist_buckets[key], edges, 0.95),
                    "p99_s": _quantile(self._hist_buckets[key], edges, 0.99),
                }
                hist.setdefault(name, {})[_labels(dict(lbl)) or "all"] = entry
        return {
            "uptime_s": round(time.time() - self.started, 1),
            "counters": counters,
            "gauges": gauges,
            # Kept under its old key so existing readers do not break.
            "latency": hist.get(DEFAULT_HISTOGRAM, {}),
            "histograms": hist,
        }

    def render_prometheus(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            by_name: dict[str, list[tuple[tuple, float]]] = defaultdict(list)
            for (name, lbl), value in self._counters.items():
                by_name[name].append((lbl, value))
            for name in sorted(by_name):
                lines.append(f"# TYPE {name} counter")
                for lbl, value in sorted(by_name[name]):
                    lines.append(f"{name}{_labels(dict(lbl))} {value:g}")

            by_gauge: dict[str, list[tuple[tuple, float]]] = defaultdict(list)
            for (name, lbl), value in self._gauges.items():
                by_gauge[name].append((lbl, value))
            for name in sorted(by_gauge):
                lines.append(f"# TYPE {name} gauge")
                for lbl, value in sorted(by_gauge[name]):
                    lines.append(f"{name}{_labels(dict(lbl))} {value:g}")

            by_hist: dict[str, list[tuple]] = defaultdict(list)
            for (name, lbl) in self._hist_count:
                by_hist[name].append(lbl)
            for name in sorted(by_hist):
                edges = HISTOGRAM_BUCKETS.get(name, LATENCY_BUCKETS)
                lines.append(f"# TYPE {name} histogram")
                for lbl in sorted(by_hist[name]):
                    key = (name, lbl)
                    base = dict(lbl)
                    cumulative = 0
                    buckets = self._hist_buckets[key]
                    for i, edge in enumerate(edges):
                        cumulative += buckets[i]
                        tagged = dict(base, le=str(edge))
                        lines.append(
                            f"{name}_bucket{_labels(tagged)} {cumulative}")
                    cumulative += buckets[-1]
                    tagged = dict(base, le="+Inf")
                    lines.append(f"{name}_bucket{_labels(tagged)} {cumulative}")
                    lines.append(
                        f"{name}_sum{_labels(base)} {self._hist_sum[key]:g}")
                    lines.append(
                        f"{name}_count{_labels(base)} {self._hist_count[key]}")
        return "\n".join(lines) + "\n"
