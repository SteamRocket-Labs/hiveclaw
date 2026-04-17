"""In-memory counters for memory backend observability.

Lightweight, zero-dep. Each operation bumps a counter, and each timed
operation records latency in an EWMA-based histogram summary.

Exposed via GET /api/admin/metrics/memory as JSON. Future: wire to
Prometheus by adding a collector that reads `snapshot()`.

Thread-safe for the level of concurrency we actually run (heartbeat +
retrieval hits are interleaved on the event loop, not preempted).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ── Counters ──────────────────────────────────────────────────

# Labels: (backend, tenant_id)
_recall_total: dict[tuple[str, str], int] = defaultdict(int)
_recall_error_total: dict[tuple[str, str, str], int] = defaultdict(int)  # + reason
_recall_empty_total: dict[tuple[str, str], int] = defaultdict(int)
_sync_items_total: dict[str, int] = defaultdict(int)  # by tenant_id
_sync_failure_total: dict[str, int] = defaultdict(int)  # by tenant_id

# Degradation state — tracks consecutive failures per tenant for
# auto-fallback policy (Phase 4).
_consecutive_failures: dict[str, int] = defaultdict(int)
_last_failure_ts: dict[str, float] = {}


@dataclass
class LatencyWindow:
    """Sliding-window latency stats (keeps last N samples)."""
    samples: list[float] = field(default_factory=list)
    max_samples: int = 256

    def observe(self, ms: float) -> None:
        self.samples.append(ms)
        if len(self.samples) > self.max_samples:
            del self.samples[: len(self.samples) - self.max_samples]

    def snapshot(self) -> dict[str, float]:
        n = len(self.samples)
        if not n:
            return {"count": 0}
        srt = sorted(self.samples)
        return {
            "count": n,
            "p50": srt[n // 2],
            "p95": srt[min(n - 1, int(n * 0.95))],
            "p99": srt[min(n - 1, int(n * 0.99))],
            "max": srt[-1],
        }


_recall_latency: dict[tuple[str, str], LatencyWindow] = defaultdict(LatencyWindow)


# ── Public API ────────────────────────────────────────────────


def record_recall(backend: str, tenant_id: str, latency_ms: float, *, empty: bool) -> None:
    key = (backend, tenant_id)
    _recall_total[key] += 1
    _recall_latency[key].observe(latency_ms)
    if empty:
        _recall_empty_total[key] += 1
    _consecutive_failures.pop(tenant_id, None)  # success resets fail streak


def record_recall_error(backend: str, tenant_id: str, reason: str) -> None:
    _recall_error_total[(backend, tenant_id, reason)] += 1
    _consecutive_failures[tenant_id] = _consecutive_failures.get(tenant_id, 0) + 1
    _last_failure_ts[tenant_id] = time.time()


def record_sync(tenant_id: str, items: int) -> None:
    _sync_items_total[tenant_id] += items


def record_sync_failure(tenant_id: str) -> None:
    _sync_failure_total[tenant_id] += 1


def consecutive_failures(tenant_id: str) -> int:
    return _consecutive_failures.get(tenant_id, 0)


def time_since_last_failure(tenant_id: str) -> float | None:
    """Seconds since the last recorded recall failure, or None if never failed."""
    ts = _last_failure_ts.get(tenant_id)
    return time.time() - ts if ts else None


def snapshot() -> dict[str, Any]:
    """Return all counters + latency summaries. Safe to serialize as JSON."""
    return {
        "recall_total": {f"{k[0]}:{k[1]}": v for k, v in _recall_total.items()},
        "recall_error_total": {
            f"{k[0]}:{k[1]}:{k[2]}": v for k, v in _recall_error_total.items()
        },
        "recall_empty_total": {f"{k[0]}:{k[1]}": v for k, v in _recall_empty_total.items()},
        "recall_latency_ms": {
            f"{k[0]}:{k[1]}": v.snapshot() for k, v in _recall_latency.items()
        },
        "sync_items_total": dict(_sync_items_total),
        "sync_failure_total": dict(_sync_failure_total),
        "consecutive_failures": dict(_consecutive_failures),
    }


def reset_all() -> None:
    """For testing — clear every counter."""
    _recall_total.clear()
    _recall_error_total.clear()
    _recall_empty_total.clear()
    _recall_latency.clear()
    _sync_items_total.clear()
    _sync_failure_total.clear()
    _consecutive_failures.clear()
    _last_failure_ts.clear()


class RecallTimer:
    """Async context manager that records latency + outcome on exit.

    Usage:
        async with RecallTimer("hindsight", tenant_hex) as t:
            results = await do_recall()
            t.observed_results = len(results)
    """

    def __init__(self, backend: str, tenant_id: str) -> None:
        self._backend = backend
        self._tenant_id = tenant_id
        self._start: float = 0.0
        self.observed_results: int | None = None
        self.error_reason: str | None = None

    async def __aenter__(self) -> "RecallTimer":
        self._start = asyncio.get_event_loop().time()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        elapsed_ms = (asyncio.get_event_loop().time() - self._start) * 1000
        if exc_type is not None:
            record_recall_error(self._backend, self._tenant_id, exc_type.__name__)
            return  # Do not suppress the exception
        if self.error_reason is not None:
            record_recall_error(self._backend, self._tenant_id, self.error_reason)
            return
        empty = (self.observed_results is None) or (self.observed_results == 0)
        record_recall(self._backend, self._tenant_id, elapsed_ms, empty=empty)
