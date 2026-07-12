"""In-memory counters for memory backend observability.

Lightweight, zero-dep. Each operation bumps a counter, and each timed
operation records latency in an EWMA-based histogram summary.

Exposed via GET /api/admin/metrics/memory as JSON and GET /metrics in
Prometheus text format.

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


# ── Legacy extraction pipeline counters (P0-2c) ─────────────────────
# The old RESPONSE_COMPLETE → schedule_extract → durable queue path is no
# longer the canonical runtime route. These counters remain for explicitly
# enabled migration/repair jobs and their replay queue:
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
# Drain timed out (legacy SESSION_CLOSE/fallback close) — task still running, entry kept.
_extract_drain_timeout_total: dict[str, int] = defaultdict(int)  # by source-of-truth ("session_close")
# Startup replay outcome (P0-2b).
_extract_replay_scheduled_total: int = 0
_extract_replay_skipped_stale_total: int = 0
_extract_replay_failed_total: int = 0


# ── Frozen prompt prefix metering (P1-1b) ─────────────────────
# build_frozen_prompt_prefix produces the cache-key boundary every
# session. If it grows beyond budget, prompt cache hit-rate degrades
# and per-call cost rises. Operators need visibility on:
#  - rolling distribution (latency-style sliding window of last N samples)
#  - count of warns (>= warn threshold) and hard-limit breaches
# Sample window so we can show p50/p95/max without unbounded memory.

_frozen_prefix_chars_window: LatencyWindow = LatencyWindow()
_frozen_prefix_tokens_window: LatencyWindow = LatencyWindow()
_frozen_prefix_warn_total: int = 0
_frozen_prefix_overrun_total: int = 0


# ── Autonomous LLM call tracking (P1-W3-10/11) ────────────────
# The dream / heartbeat / skill_distiller paths spend tenant token
# budget without going through the invoke_agent governance pipeline.
# These counters surface the call volume so operators have at minimum a
# rate signal — separate from agent-initiated LLM usage.
_autonomous_llm_calls_total: dict[tuple[str, str], int] = defaultdict(int)  # (source, outcome)


# ── LLM output cap tracking (A5) ──────────────────────────────
# Provider responses with finish_reason=length/max_tokens mean the model hit
# its output ceiling. Track them centrally so non-agent background LLM paths
# surface truncation pressure instead of failing silently.
_llm_output_cap_hit_total: dict[tuple[str, str, str, str, str], int] = defaultdict(int)

# ── Runtime hook failures (P1-12) ─────────────────────────────
# Hook failures are intentionally non-fatal, but they must be visible because
# RESPONSE_COMPLETE / PRE_COMPACTION / POST_COMPACTION drive memory durability.
_hook_failure_total: dict[tuple[str, str, str], int] = defaultdict(int)
_memory_context_status_total: dict[tuple[str, str], int] = defaultdict(int)

# ── Prompt cache metrics (P1 cache observability) ──────────────
# Provider usage payloads already expose cache read/write tokens; keep these
# counters provider-scoped so operators can see whether cache-stable prompt
# work is actually paying off in production.
_prompt_cache_observations_total: dict[tuple[str, str], int] = defaultdict(int)  # (provider, hit|miss)
_prompt_cache_read_tokens_total: dict[str, int] = defaultdict(int)
_prompt_cache_write_tokens_total: dict[str, int] = defaultdict(int)
_prompt_cache_uncached_input_tokens_total: dict[str, int] = defaultdict(int)
_prompt_cache_input_tokens_total: dict[str, int] = defaultdict(int)

# ── Invocation trace metrics ──────────────────────────────────
# Persisted invocation_spans are the durable query surface; these in-process
# counters are the scrape-friendly signal for liveness, latency, and token burn.
_invocation_spans_total: dict[tuple[str, str], int] = defaultdict(int)  # (span_type, status)
_invocation_span_duration_ms: dict[tuple[str, str], LatencyWindow] = defaultdict(LatencyWindow)
_invocation_tokens_total: dict[tuple[str, str, str], int] = defaultdict(int)  # (provider, model, source)
_heartbeat_reflection_total: dict[str, int] = defaultdict(int)

_EXTRACT_FAILURE_RATIO_ALERT_THRESHOLD = 0.20
_EXTRACT_FAILURE_RATIO_MIN_EVENTS = 5


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
        "recall_error_total": {f"{k[0]}:{k[1]}:{k[2]}": v for k, v in _recall_error_total.items()},
        "recall_empty_total": {f"{k[0]}:{k[1]}": v for k, v in _recall_empty_total.items()},
        "recall_latency_ms": {f"{k[0]}:{k[1]}": v.snapshot() for k, v in _recall_latency.items()},
        "sync_items_total": dict(_sync_items_total),
        "sync_failure_total": dict(_sync_failure_total),
        "consecutive_failures": dict(_consecutive_failures),
        # P0-2c extraction pipeline counters
        "extract_enqueue_total": dict(_extract_enqueue_total),
        "extract_enqueue_failure_total": {f"{k[0]}:{k[1]}": v for k, v in _extract_enqueue_failure_total.items()},
        "extract_task_success_total": dict(_extract_task_success_total),
        "extract_task_failure_total": {f"{k[0]}:{k[1]}": v for k, v in _extract_task_failure_total.items()},
        "extract_drain_timeout_total": dict(_extract_drain_timeout_total),
        "extract_replay": {
            "scheduled": _extract_replay_scheduled_total,
            "skipped_stale": _extract_replay_skipped_stale_total,
            "failed": _extract_replay_failed_total,
        },
        # P1-1b frozen prompt prefix metering
        "frozen_prefix_chars": _frozen_prefix_chars_window.snapshot(),
        "frozen_prefix_tokens": _frozen_prefix_tokens_window.snapshot(),
        "frozen_prefix_warn_total": _frozen_prefix_warn_total,
        "frozen_prefix_overrun_total": _frozen_prefix_overrun_total,
        # P1-W3-10/11 autonomous LLM call tracking
        "autonomous_llm_calls_total": {f"{k[0]}:{k[1]}": v for k, v in _autonomous_llm_calls_total.items()},
        "llm_output_cap_hit_total": {
            f"{k[0]}:{k[1]}:{k[2]}:{k[3]}:{k[4]}": v for k, v in _llm_output_cap_hit_total.items()
        },
        "hook_failure_total": {f"{k[0]}:{k[1]}:{k[2]}": v for k, v in _hook_failure_total.items()},
        "memory_context_status_total": {f"{k[0]}:{k[1]}": v for k, v in _memory_context_status_total.items()},
        "prompt_cache_observations_total": {f"{k[0]}:{k[1]}": v for k, v in _prompt_cache_observations_total.items()},
        "prompt_cache_read_tokens_total": dict(_prompt_cache_read_tokens_total),
        "prompt_cache_write_tokens_total": dict(_prompt_cache_write_tokens_total),
        "prompt_cache_uncached_input_tokens_total": dict(_prompt_cache_uncached_input_tokens_total),
        "prompt_cache_input_tokens_total": dict(_prompt_cache_input_tokens_total),
        "prompt_cache_hit_rate": {
            provider: (_prompt_cache_read_tokens_total[provider] / total if total else 0.0)
            for provider, total in _prompt_cache_input_tokens_total.items()
        },
        "invocation_spans_total": {f"{k[0]}:{k[1]}": v for k, v in _invocation_spans_total.items()},
        "invocation_span_duration_ms": {
            f"{k[0]}:{k[1]}": v.snapshot() for k, v in _invocation_span_duration_ms.items()
        },
        "invocation_tokens_total": {f"{k[0]}:{k[1]}:{k[2]}": v for k, v in _invocation_tokens_total.items()},
        "heartbeat_reflection_total": dict(_heartbeat_reflection_total),
    }


def _escape_prometheus_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _prometheus_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{key}="{_escape_prometheus_label(value)}"' for key, value in labels.items()]
    return "{" + ",".join(parts) + "}"


def _prometheus_number(value: int | float) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, ".12g")
    return str(value)


def _append_prometheus_metric(
    lines: list[str],
    *,
    name: str,
    metric_type: str,
    help_text: str,
    samples: list[tuple[dict[str, str], int | float]],
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")
    for labels, value in sorted(samples, key=lambda item: tuple(sorted(item[0].items()))):
        lines.append(f"{name}{_prometheus_labels(labels)} {_prometheus_number(value)}")


def _latency_window_samples(windows: dict[tuple[str, str], LatencyWindow]) -> list[tuple[dict[str, str], int | float]]:
    samples: list[tuple[dict[str, str], int | float]] = []
    for (backend, tenant_id), window in windows.items():
        snap = window.snapshot()
        for stat_name, value in snap.items():
            samples.append(
                (
                    {
                        "backend": backend,
                        "tenant_id": tenant_id,
                        "stat": stat_name,
                    },
                    value,
                )
            )
    return samples


def _invocation_duration_samples() -> list[tuple[dict[str, str], int | float]]:
    samples: list[tuple[dict[str, str], int | float]] = []
    for (span_type, status), window in _invocation_span_duration_ms.items():
        snap = window.snapshot()
        for stat_name, value in snap.items():
            samples.append(
                (
                    {
                        "span_type": span_type,
                        "status": status,
                        "stat": stat_name,
                    },
                    value,
                )
            )
    return samples


def _extract_failure_ratio_samples() -> tuple[
    list[tuple[dict[str, str], int | float]],
    list[tuple[dict[str, str], int | float]],
]:
    successes_by_source = dict(_extract_task_success_total)
    failures_by_source: dict[str, int] = defaultdict(int)
    for (source, _reason), count in _extract_task_failure_total.items():
        failures_by_source[source] += count

    ratio_samples: list[tuple[dict[str, str], int | float]] = []
    alert_samples: list[tuple[dict[str, str], int | float]] = []
    for source in sorted(set(successes_by_source) | set(failures_by_source)):
        success_count = successes_by_source.get(source, 0)
        failure_count = failures_by_source.get(source, 0)
        total = success_count + failure_count
        ratio = failure_count / total if total else 0.0
        alert = int(total >= _EXTRACT_FAILURE_RATIO_MIN_EVENTS and ratio >= _EXTRACT_FAILURE_RATIO_ALERT_THRESHOLD)
        labels = {"source": source}
        ratio_samples.append((labels, ratio))
        alert_samples.append((labels, alert))
    return ratio_samples, alert_samples


def render_prometheus() -> str:
    """Render a zero-dependency Prometheus text exposition snapshot."""
    lines: list[str] = []

    _append_prometheus_metric(
        lines,
        name="hive_memory_up",
        metric_type="gauge",
        help_text="Memory metrics exporter health.",
        samples=[({}, 1)],
    )
    try:
        from app.services.daemon_liveness import daemon_liveness_snapshot

        daemon_rows = daemon_liveness_snapshot()
    except Exception:
        daemon_rows = {}
    _append_prometheus_metric(
        lines,
        name="hive_daemon_liveness_up",
        metric_type="gauge",
        help_text="One when a registered background daemon is live, zero when degraded/crashed/stopped.",
        samples=[({"name": name}, int(bool(row.get("healthy")))) for name, row in daemon_rows.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_daemon_last_heartbeat_age_seconds",
        metric_type="gauge",
        help_text="Seconds since the daemon last completed or reported a tick.",
        samples=[
            ({"name": name}, row["last_heartbeat_age_seconds"])
            for name, row in daemon_rows.items()
            if row.get("last_heartbeat_age_seconds") is not None
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_daemon_tick_total",
        metric_type="counter",
        help_text="Total successful daemon ticks observed in this process.",
        samples=[({"name": name}, int(row.get("tick_count") or 0)) for name, row in daemon_rows.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_daemon_error_total",
        metric_type="counter",
        help_text="Total non-fatal daemon tick errors observed in this process.",
        samples=[({"name": name}, int(row.get("error_count") or 0)) for name, row in daemon_rows.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_daemon_crash_total",
        metric_type="counter",
        help_text="Total daemon task crashes observed in this process.",
        samples=[({"name": name}, int(row.get("crash_count") or 0)) for name, row in daemon_rows.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_recall_total",
        metric_type="counter",
        help_text="Total memory recall attempts.",
        samples=[
            ({"backend": backend, "tenant_id": tenant_id}, count)
            for (backend, tenant_id), count in _recall_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_recall_error_total",
        metric_type="counter",
        help_text="Total memory recall errors.",
        samples=[
            ({"backend": backend, "tenant_id": tenant_id, "reason": reason}, count)
            for (backend, tenant_id, reason), count in _recall_error_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_recall_empty_total",
        metric_type="counter",
        help_text="Total empty memory recall responses.",
        samples=[
            ({"backend": backend, "tenant_id": tenant_id}, count)
            for (backend, tenant_id), count in _recall_empty_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_recall_latency_ms",
        metric_type="gauge",
        help_text="Sliding-window memory recall latency in milliseconds.",
        samples=_latency_window_samples(_recall_latency),
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_sync_items_total",
        metric_type="counter",
        help_text="Total memory sync items processed.",
        samples=[({"tenant_id": tenant_id}, count) for tenant_id, count in _sync_items_total.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_sync_failure_total",
        metric_type="counter",
        help_text="Total memory sync failures.",
        samples=[({"tenant_id": tenant_id}, count) for tenant_id, count in _sync_failure_total.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_consecutive_failures",
        metric_type="gauge",
        help_text="Current consecutive memory recall failures by tenant.",
        samples=[({"tenant_id": tenant_id}, count) for tenant_id, count in _consecutive_failures.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_extract_enqueue_total",
        metric_type="counter",
        help_text="Total durable memory extraction enqueue successes.",
        samples=[({"source": source}, count) for source, count in _extract_enqueue_total.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_extract_enqueue_failure_total",
        metric_type="counter",
        help_text="Total durable memory extraction enqueue failures.",
        samples=[
            ({"source": source, "reason": reason}, count)
            for (source, reason), count in _extract_enqueue_failure_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_extract_task_success_total",
        metric_type="counter",
        help_text="Total durable memory extraction task successes.",
        samples=[({"source": source}, count) for source, count in _extract_task_success_total.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_extract_task_failure_total",
        metric_type="counter",
        help_text="Total durable memory extraction task failures.",
        samples=[
            ({"source": source, "reason": reason}, count)
            for (source, reason), count in _extract_task_failure_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_extract_drain_timeout_total",
        metric_type="counter",
        help_text="Total durable memory extraction drain timeouts.",
        samples=[({"source": source}, count) for source, count in _extract_drain_timeout_total.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_extract_replay_total",
        metric_type="counter",
        help_text="Total durable memory extraction replay outcomes.",
        samples=[
            ({"outcome": "scheduled"}, _extract_replay_scheduled_total),
            ({"outcome": "skipped_stale"}, _extract_replay_skipped_stale_total),
            ({"outcome": "failed"}, _extract_replay_failed_total),
        ],
    )
    ratio_samples, alert_samples = _extract_failure_ratio_samples()
    _append_prometheus_metric(
        lines,
        name="hive_memory_extract_failure_ratio",
        metric_type="gauge",
        help_text="Durable memory extraction task failure ratio by source.",
        samples=ratio_samples,
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_extract_failure_ratio_high",
        metric_type="gauge",
        help_text="One when extraction failure ratio is at or above the alert threshold.",
        samples=alert_samples,
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_frozen_prefix_chars",
        metric_type="gauge",
        help_text="Sliding-window frozen prompt prefix character count.",
        samples=[({"stat": stat_name}, value) for stat_name, value in _frozen_prefix_chars_window.snapshot().items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_frozen_prefix_tokens",
        metric_type="gauge",
        help_text="Sliding-window frozen prompt prefix token estimate.",
        samples=[({"stat": stat_name}, value) for stat_name, value in _frozen_prefix_tokens_window.snapshot().items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_frozen_prefix_warn_total",
        metric_type="counter",
        help_text="Total frozen prompt prefix warning threshold breaches.",
        samples=[({}, _frozen_prefix_warn_total)],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_frozen_prefix_overrun_total",
        metric_type="counter",
        help_text="Total frozen prompt prefix hard-limit overruns.",
        samples=[({}, _frozen_prefix_overrun_total)],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_autonomous_llm_calls_total",
        metric_type="counter",
        help_text="Total autonomous memory-related LLM calls outside invoke_agent.",
        samples=[
            ({"source": source, "outcome": outcome}, count)
            for (source, outcome), count in _autonomous_llm_calls_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_llm_output_cap_hit_total",
        metric_type="counter",
        help_text="Total LLM output-cap hits by provider, model, mode, and phase.",
        samples=[
            (
                {
                    "provider": provider,
                    "model": model,
                    "finish_reason": finish_reason,
                    "mode": mode,
                    "phase": phase,
                },
                count,
            )
            for (provider, model, finish_reason, mode, phase), count in _llm_output_cap_hit_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_hook_failure_total",
        metric_type="counter",
        help_text="Total non-fatal runtime hook failures.",
        samples=[
            ({"event": event, "source": source, "reason": reason}, count)
            for (event, source, reason), count in _hook_failure_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_context_status_total",
        metric_type="counter",
        help_text="Total typed memory context outcomes by status and code.",
        samples=[
            ({"status": status, "code": code}, count) for (status, code), count in _memory_context_status_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_prompt_cache_observations_total",
        metric_type="counter",
        help_text="Total prompt cache observations by provider and cache hit outcome.",
        samples=[
            ({"provider": provider, "outcome": outcome}, count)
            for (provider, outcome), count in _prompt_cache_observations_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_prompt_cache_read_tokens_total",
        metric_type="counter",
        help_text="Total prompt cache read tokens by provider.",
        samples=[({"provider": provider}, count) for provider, count in _prompt_cache_read_tokens_total.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_prompt_cache_write_tokens_total",
        metric_type="counter",
        help_text="Total prompt cache write tokens by provider.",
        samples=[({"provider": provider}, count) for provider, count in _prompt_cache_write_tokens_total.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_prompt_cache_input_tokens_total",
        metric_type="counter",
        help_text="Total prompt input tokens observed for cache metrics by provider.",
        samples=[({"provider": provider}, count) for provider, count in _prompt_cache_input_tokens_total.items()],
    )
    _append_prometheus_metric(
        lines,
        name="hive_prompt_cache_uncached_input_tokens_total",
        metric_type="counter",
        help_text="Total uncached prompt input tokens observed by provider.",
        samples=[
            ({"provider": provider}, count) for provider, count in _prompt_cache_uncached_input_tokens_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_prompt_cache_hit_rate",
        metric_type="gauge",
        help_text="Prompt cache read-token ratio by provider.",
        samples=[
            (
                {"provider": provider},
                _prompt_cache_read_tokens_total[provider] / total if total else 0.0,
            )
            for provider, total in _prompt_cache_input_tokens_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_invocation_spans_total",
        metric_type="counter",
        help_text="Total runtime invocation spans observed in this process.",
        samples=[
            ({"span_type": span_type, "status": status}, count)
            for (span_type, status), count in _invocation_spans_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_invocation_span_duration_ms",
        metric_type="gauge",
        help_text="Sliding-window runtime span duration in milliseconds.",
        samples=_invocation_duration_samples(),
    )
    _append_prometheus_metric(
        lines,
        name="hive_invocation_tokens_total",
        metric_type="counter",
        help_text="Total tokens observed from invocation span usage payloads.",
        samples=[
            ({"provider": provider, "model": model, "source": source}, count)
            for (provider, model, source), count in _invocation_tokens_total.items()
        ],
    )
    _append_prometheus_metric(
        lines,
        name="hive_memory_heartbeat_reflection_total",
        metric_type="counter",
        help_text="Total heartbeat reflection learning outcomes.",
        samples=[({"outcome": outcome}, count) for outcome, count in _heartbeat_reflection_total.items()],
    )

    return "\n".join(lines) + "\n"


def reset_all() -> None:
    """For testing — clear every counter."""
    global _extract_replay_scheduled_total, _extract_replay_skipped_stale_total, _extract_replay_failed_total
    global _frozen_prefix_warn_total, _frozen_prefix_overrun_total
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
    _frozen_prefix_chars_window.samples.clear()
    _frozen_prefix_tokens_window.samples.clear()
    _frozen_prefix_warn_total = 0
    _frozen_prefix_overrun_total = 0
    _autonomous_llm_calls_total.clear()
    _llm_output_cap_hit_total.clear()
    _hook_failure_total.clear()
    _memory_context_status_total.clear()
    _prompt_cache_observations_total.clear()
    _prompt_cache_read_tokens_total.clear()
    _prompt_cache_write_tokens_total.clear()
    _prompt_cache_uncached_input_tokens_total.clear()
    _prompt_cache_input_tokens_total.clear()
    _invocation_spans_total.clear()
    _invocation_span_duration_ms.clear()
    _invocation_tokens_total.clear()
    _heartbeat_reflection_total.clear()


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


def record_heartbeat_reflection(outcome: str) -> None:
    normalized = str(outcome or "unknown").strip().lower() or "unknown"
    _heartbeat_reflection_total[normalized] += 1


# ── Frozen prefix recorders (P1-1b) ───────────────────────────


def record_autonomous_llm_call(*, source: str, outcome: str) -> None:
    """Bump an autonomous LLM call counter.

    `source` is one of {"dream", "heartbeat", "skill_distiller", "fast_reflection_learning_brain"}.
    `outcome` is {"success", "failure", "skipped"} so dashboards can
    chart success rate alongside volume.
    """
    _autonomous_llm_calls_total[(source, outcome)] += 1


def record_llm_output_cap_hit(*, provider: str, model: str, finish_reason: str, mode: str, phase: str) -> None:
    """Bump the provider/model output-cap counter.

    `mode` is {"complete", "stream"} and `phase` is {"initial", "retry"}.
    """
    _llm_output_cap_hit_total[
        (
            provider or "unknown",
            model or "unknown",
            finish_reason or "unknown",
            mode or "unknown",
            phase or "unknown",
        )
    ] += 1


def record_hook_failure(*, event: str, source: str, reason: str) -> None:
    """Bump a non-fatal runtime hook failure counter."""
    _hook_failure_total[(event or "unknown", source or "unknown", reason or "unknown")] += 1


def record_memory_context_status(*, status: str, code: str) -> None:
    _memory_context_status_total[(status or "unknown", code or "unknown")] += 1


def record_invocation_span_metric(
    *,
    span_type: str,
    status: str,
    duration_ms: float,
    usage: dict[str, Any] | None = None,
    provider: str = "unknown",
    model: str = "unknown",
    source: str = "unknown",
) -> None:
    """Record one runtime span metric sample."""
    clean_span_type = span_type or "unknown"
    clean_status = status or "unknown"
    _invocation_spans_total[(clean_span_type, clean_status)] += 1
    _invocation_span_duration_ms[(clean_span_type, clean_status)].observe(max(0.0, float(duration_ms or 0.0)))

    token_count = 0
    if isinstance(usage, dict):
        from app.services.token_tracker import extract_usage_tokens

        token_count = int(extract_usage_tokens(usage) or 0)
    if token_count > 0:
        _invocation_tokens_total[(provider or "unknown", model or "unknown", source or "unknown")] += token_count


def record_prompt_cache_metrics(metrics: Any) -> None:
    """Record one provider cache observation extracted from an LLM usage payload."""
    provider = str(getattr(metrics, "provider", "") or "unknown")
    outcome = "hit" if bool(getattr(metrics, "cache_hit", False)) else "miss"
    _prompt_cache_observations_total[(provider, outcome)] += 1
    _prompt_cache_read_tokens_total[provider] += int(getattr(metrics, "cache_read_tokens", 0) or 0)
    _prompt_cache_write_tokens_total[provider] += int(getattr(metrics, "cache_write_tokens", 0) or 0)
    _prompt_cache_uncached_input_tokens_total[provider] += int(getattr(metrics, "uncached_input_tokens", 0) or 0)
    _prompt_cache_input_tokens_total[provider] += int(getattr(metrics, "total_input_tokens", 0) or 0)


def record_frozen_prefix_metering(*, chars: int, tokens: int, warn: bool = False, overrun: bool = False) -> None:
    """Record one frozen-prefix build sample.

    `warn` and `overrun` are independent flags — overrun implies warn but
    callers signal both explicitly so snapshots reflect actual breach severity.
    """
    global _frozen_prefix_warn_total, _frozen_prefix_overrun_total
    _frozen_prefix_chars_window.observe(float(chars))
    _frozen_prefix_tokens_window.observe(float(tokens))
    if warn:
        _frozen_prefix_warn_total += 1
    if overrun:
        _frozen_prefix_overrun_total += 1


class RecallTimer:
    """Async context manager that records latency + outcome on exit.

    Usage:
        async with RecallTimer("native", tenant_hex) as t:
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
