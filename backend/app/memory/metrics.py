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


# ── Extraction pipeline counters (P0-2c) ─────────────────────
# The hot-path RESPONSE_COMPLETE → schedule_extract → durable queue path
# fails silently in several ways that operators previously couldn't see:
#  - enqueue itself fails (FS full / permission denied)
#  - scheduled task raises after pattern fallback (kept on disk for replay)
#  - drain timeout cancels in-flight work
#  - startup replay re-schedules and may hit any of the above again
# Each counter is keyed by (source, reason) where useful.

# Successful enqueue (payload persisted before task starts).
_extract_enqueue_total: dict[str, int] = defaultdict(int)  # by source
# enqueue() raised — durability disabled for this batch.
_extract_enqueue_failure_total: dict[tuple[str, str], int] = defaultdict(int)  # (source, reason)
# Task completed cleanly (mark_done called).
_extract_task_success_total: dict[str, int] = defaultdict(int)  # by source
# Task raised after pattern fallback — entry left for replay.
_extract_task_failure_total: dict[tuple[str, str], int] = defaultdict(int)  # (source, exc_type)
# Drain timed out (SESSION_CLOSE) — task still running, entry kept.
_extract_drain_timeout_total: dict[str, int] = defaultdict(int)  # by source-of-truth ("session_close")
# Startup replay outcome (P0-2b).
_extract_replay_scheduled_total: int = 0
_extract_replay_skipped_stale_total: int = 0
_extract_replay_failed_total: int = 0


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
        # P0-2c extraction pipeline counters
        "extract_enqueue_total": dict(_extract_enqueue_total),
        "extract_enqueue_failure_total": {
            f"{k[0]}:{k[1]}": v for k, v in _extract_enqueue_failure_total.items()
        },
        "extract_task_success_total": dict(_extract_task_success_total),
        "extract_task_failure_total": {
            f"{k[0]}:{k[1]}": v for k, v in _extract_task_failure_total.items()
        },
        "extract_drain_timeout_total": dict(_extract_drain_timeout_total),
        "extract_replay": {
            "scheduled": _extract_replay_scheduled_total,
            "skipped_stale": _extract_replay_skipped_stale_total,
            "failed": _extract_replay_failed_total,
        },
    }


def reset_all() -> None:
    """For testing — clear every counter."""
    global _extract_replay_scheduled_total, _extract_replay_skipped_stale_total, _extract_replay_failed_total
    _recall_total.clear()
    _recall_error_total.clear()
    _recall_empty_total.clear()
    _recall_latency.clear()
    _sync_items_total.clear()
    _sync_failure_total.clear()
    _consecutive_failures.clear()
    _last_failure_ts.clear()
    _extract_enqueue_total.clear()
    _extract_enqueue_failure_total.clear()
    _extract_task_success_total.clear()
    _extract_task_failure_total.clear()
    _extract_drain_timeout_total.clear()
    _extract_replay_scheduled_total = 0
    _extract_replay_skipped_stale_total = 0
    _extract_replay_failed_total = 0


# ── Extraction recorders (P0-2c) ──────────────────────────────


def record_extract_enqueue(source: str) -> None:
    _extract_enqueue_total[source] += 1


def record_extract_enqueue_failure(source: str, reason: str) -> None:
    """Reason is exception class name; bucketed for cardinality control."""
    _extract_enqueue_failure_total[(source, reason)] += 1


def record_extract_task_success(source: str) -> None:
    _extract_task_success_total[source] += 1


def record_extract_task_failure(source: str, exc_type: str) -> None:
    _extract_task_failure_total[(source, exc_type)] += 1


def record_extract_drain_timeout(source: str = "session_close") -> None:
    _extract_drain_timeout_total[source] += 1


def record_extract_replay_outcome(*, scheduled: int, skipped_stale: int, failed: int) -> None:
    """Single call from main.py lifespan after replay completes."""
    global _extract_replay_scheduled_total, _extract_replay_skipped_stale_total, _extract_replay_failed_total
    _extract_replay_scheduled_total += scheduled
    _extract_replay_skipped_stale_total += skipped_stale
    _extract_replay_failed_total += failed


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
