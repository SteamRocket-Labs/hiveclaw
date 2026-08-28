"""Low-cardinality process metrics for durable result fan-in."""

from __future__ import annotations

from collections import Counter
from threading import Lock


_RESULTS: Counter[tuple[str, str]] = Counter()
_PAGES: Counter[tuple[str, str]] = Counter()
_ITEMS: Counter[tuple[str, str]] = Counter()
_LOCK = Lock()
_SOURCE_KINDS = frozenset(
    {
        "subagent",
        "agent_team",
        "workflow",
        "trigger",
        "delegation",
        "a2a_delegation",
        "a2a_continuation",
        "runtime_budget",
        "approval",
    }
)
_DELIVERY_MODES = frozenset({"parent_continuation", "session_projection"})
_OUTCOMES = frozenset({"prepared", "delivered", "retry", "deferred", "dead_letter"})


def _bounded(value: str, allowed: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else "other"


def record_runtime_result_observed(*, source_kind: str, size_bytes: int) -> None:
    source = _bounded(source_kind, _SOURCE_KINDS)
    with _LOCK:
        _RESULTS[(source, "count")] += 1
        _RESULTS[(source, "bytes")] += max(0, int(size_bytes))


def record_runtime_result_page(*, delivery_mode: str, outcome: str, item_count: int) -> None:
    mode = _bounded(delivery_mode, _DELIVERY_MODES)
    status = _bounded(outcome, _OUTCOMES)
    with _LOCK:
        _PAGES[(mode, status)] += 1
        _ITEMS[(mode, status)] += max(0, int(item_count))


def reset_runtime_result_metrics() -> None:
    with _LOCK:
        _RESULTS.clear()
        _PAGES.clear()
        _ITEMS.clear()


def render_runtime_result_prometheus() -> str:
    lines = [
        "# HELP runtime_results_observed_total Durable runtime results first observed by the delivery worker.",
        "# TYPE runtime_results_observed_total counter",
        "# HELP runtime_result_bytes_observed_total Bytes in durable runtime results first observed by the delivery worker.",
        "# TYPE runtime_result_bytes_observed_total counter",
    ]
    with _LOCK:
        results = dict(_RESULTS)
        pages = dict(_PAGES)
        items = dict(_ITEMS)
    sources = sorted({source for source, measure in results if measure == "count"})
    for source in sources:
        lines.append(f'runtime_results_observed_total{{source_kind="{source}"}} {results[(source, "count")]}')
        lines.append(f'runtime_result_bytes_observed_total{{source_kind="{source}"}} {results[(source, "bytes")]}')
    lines.extend(
        [
            "# HELP runtime_result_integration_pages_total Durable integration pages by outcome.",
            "# TYPE runtime_result_integration_pages_total counter",
        ]
    )
    for (mode, outcome), count in sorted(pages.items()):
        lines.append(f'runtime_result_integration_pages_total{{delivery_mode="{mode}",outcome="{outcome}"}} {count}')
    lines.extend(
        [
            "# HELP runtime_result_integration_items_total Result refs covered by integration page outcomes.",
            "# TYPE runtime_result_integration_items_total counter",
        ]
    )
    for (mode, outcome), count in sorted(items.items()):
        lines.append(f'runtime_result_integration_items_total{{delivery_mode="{mode}",outcome="{outcome}"}} {count}')
    return "\n".join(lines) + "\n"
