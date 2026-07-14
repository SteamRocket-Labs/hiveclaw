from __future__ import annotations

from collections import Counter, defaultdict
from threading import Lock


_LOCK = Lock()
_REQUESTS: Counter[tuple[str, str, str]] = Counter()
_CACHE_HITS: Counter[str] = Counter()
_FAILURES: Counter[tuple[str, str]] = Counter()
_RENDER_DURATION_SUM: defaultdict[tuple[str, str], float] = defaultdict(float)
_RENDER_DURATION_COUNT: Counter[tuple[str, str]] = Counter()
_OUTPUT_BYTES_SUM: Counter[tuple[str, str]] = Counter()
_OUTPUT_BYTES_COUNT: Counter[tuple[str, str]] = Counter()


def record_office_preview(
    *,
    source_kind: str,
    preview_mode: str,
    status: str,
    office_format: str,
    duration_seconds: float,
    output_bytes: int,
    cache_hit: bool,
    error_code: str | None,
) -> None:
    with _LOCK:
        _REQUESTS[(source_kind, preview_mode, status)] += 1
        _RENDER_DURATION_SUM[(office_format, preview_mode)] += max(0.0, float(duration_seconds))
        _RENDER_DURATION_COUNT[(office_format, preview_mode)] += 1
        _OUTPUT_BYTES_SUM[(office_format, preview_mode)] += max(0, int(output_bytes))
        _OUTPUT_BYTES_COUNT[(office_format, preview_mode)] += 1
        if cache_hit:
            _CACHE_HITS[source_kind] += 1
        if error_code:
            _FAILURES[(error_code, office_format)] += 1


def reset_office_preview_metrics() -> None:
    with _LOCK:
        _REQUESTS.clear()
        _CACHE_HITS.clear()
        _FAILURES.clear()
        _RENDER_DURATION_SUM.clear()
        _RENDER_DURATION_COUNT.clear()
        _OUTPUT_BYTES_SUM.clear()
        _OUTPUT_BYTES_COUNT.clear()


def snapshot_office_preview_metrics() -> dict:
    with _LOCK:
        return {
            "requests_total": {":".join(key): value for key, value in _REQUESTS.items()},
            "cache_hits_total": dict(_CACHE_HITS),
            "failures_total": {":".join(key): value for key, value in _FAILURES.items()},
            "render_seconds": {
                ":".join(key): {
                    "count": _RENDER_DURATION_COUNT[key],
                    "sum": value,
                }
                for key, value in _RENDER_DURATION_SUM.items()
            },
            "output_bytes": {
                ":".join(key): {
                    "count": _OUTPUT_BYTES_COUNT[key],
                    "sum": value,
                }
                for key, value in _OUTPUT_BYTES_SUM.items()
            },
        }


def _labels(**labels: str) -> str:
    escaped = []
    for key, value in labels.items():
        safe = str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        escaped.append(f'{key}="{safe}"')
    return "{" + ",".join(escaped) + "}" if escaped else ""


def render_office_preview_prometheus() -> str:
    with _LOCK:
        lines = [
            "# HELP office_preview_requests_total Total authenticated Office preview requests.",
            "# TYPE office_preview_requests_total counter",
        ]
        for (source_kind, preview_mode, status), count in sorted(_REQUESTS.items()):
            lines.append(
                "office_preview_requests_total"
                f"{_labels(source_kind=source_kind, preview_mode=preview_mode, status=status)} {count}"
            )
        lines.extend(
            [
                "# HELP office_preview_render_seconds Office preview render duration.",
                "# TYPE office_preview_render_seconds summary",
            ]
        )
        for (office_format, preview_mode), total in sorted(_RENDER_DURATION_SUM.items()):
            labels = _labels(format=office_format, preview_mode=preview_mode)
            lines.append(f"office_preview_render_seconds_sum{labels} {total:.12g}")
            lines.append(f"office_preview_render_seconds_count{labels} {_RENDER_DURATION_COUNT[(office_format, preview_mode)]}")
        lines.extend(
            [
                "# HELP office_preview_cache_hits_total Total Office preview cache hits.",
                "# TYPE office_preview_cache_hits_total counter",
            ]
        )
        for source_kind, count in sorted(_CACHE_HITS.items()):
            lines.append(f"office_preview_cache_hits_total{_labels(source_kind=source_kind)} {count}")
        lines.extend(
            [
                "# HELP office_preview_failures_total Total typed Office preview failures.",
                "# TYPE office_preview_failures_total counter",
            ]
        )
        for (error_code, office_format), count in sorted(_FAILURES.items()):
            lines.append(
                f"office_preview_failures_total{_labels(error_code=error_code, format=office_format)} {count}"
            )
        lines.extend(
            [
                "# HELP office_preview_output_bytes Office preview output byte totals.",
                "# TYPE office_preview_output_bytes summary",
            ]
        )
        for (office_format, preview_mode), total in sorted(_OUTPUT_BYTES_SUM.items()):
            labels = _labels(format=office_format, preview_mode=preview_mode)
            lines.append(f"office_preview_output_bytes_sum{labels} {total}")
            lines.append(f"office_preview_output_bytes_count{labels} {_OUTPUT_BYTES_COUNT[(office_format, preview_mode)]}")
        return "\n".join(lines) + "\n"
