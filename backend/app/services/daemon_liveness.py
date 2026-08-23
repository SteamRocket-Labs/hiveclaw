"""In-process liveness registry for long-running background daemons.

This registry is intentionally lightweight and process-local. It is not an
audit log; it is the live health surface that `/api/health` and `/metrics`
read so a crashed or stuck daemon does not look like a healthy API process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any


@dataclass(slots=True)
class DaemonRecord:
    name: str
    state: str = "registered"
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_success_at: datetime | None = None
    last_outcome_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    tick_count: int = 0
    outcome_count: int = 0
    error_count: int = 0
    crash_count: int = 0


_LOCK = RLock()
_DAEMONS: dict[str, DaemonRecord] = {}
_HEALTHY_STATES = {"registered", "running"}


def _now() -> datetime:
    return datetime.now(UTC)


def _record(name: str) -> DaemonRecord:
    record = _DAEMONS.get(name)
    if record is None:
        record = DaemonRecord(name=name)
        _DAEMONS[name] = record
    return record


def register_daemon(name: str) -> None:
    with _LOCK:
        _record(name)


def mark_daemon_started(name: str) -> None:
    with _LOCK:
        record = _record(name)
        now = _now()
        record.state = "running"
        record.started_at = record.started_at or now


def mark_daemon_tick(name: str) -> None:
    """Record that the loop completed an iteration.

    A tick is a liveness heartbeat, **not** a result. It deliberately does not
    touch ``last_success_at``: the trigger daemon reported ``healthy=true`` with
    87,928 ticks while producing zero terminal trigger outcomes for 38 days
    (2026-07-16 → 2026-08-23), because a spinning loop was being counted as
    success. Real work reports through :func:`mark_daemon_outcome`.
    """
    with _LOCK:
        record = _record(name)
        now = _now()
        record.state = "running"
        record.started_at = record.started_at or now
        record.last_heartbeat_at = now
        record.last_error = None
        record.tick_count += 1


def mark_daemon_outcome(name: str) -> None:
    """Record that the daemon actually drove a unit of work to a terminal state."""
    with _LOCK:
        record = _record(name)
        now = _now()
        record.started_at = record.started_at or now
        record.last_outcome_at = now
        record.last_success_at = now
        record.outcome_count += 1


def mark_daemon_error(name: str, exc: BaseException | str) -> None:
    with _LOCK:
        record = _record(name)
        now = _now()
        record.state = "error"
        record.started_at = record.started_at or now
        record.last_heartbeat_at = now
        record.last_error_at = now
        record.last_error = str(exc)
        record.error_count += 1


def mark_daemon_crashed(name: str, exc: BaseException | str) -> None:
    with _LOCK:
        record = _record(name)
        now = _now()
        record.state = "crashed"
        record.started_at = record.started_at or now
        record.last_error_at = now
        record.last_error = str(exc)
        record.crash_count += 1


def mark_daemon_stopped(name: str, reason: str = "task exited") -> None:
    with _LOCK:
        record = _record(name)
        now = _now()
        record.state = "stopped"
        record.started_at = record.started_at or now
        record.last_error_at = now
        record.last_error = reason


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def daemon_liveness_snapshot() -> dict[str, dict[str, Any]]:
    now = _now()
    with _LOCK:
        rows: dict[str, dict[str, Any]] = {}
        for name, record in sorted(_DAEMONS.items()):
            age = None
            if record.last_heartbeat_at is not None:
                age = max(0.0, (now - record.last_heartbeat_at).total_seconds())
            rows[name] = {
                "name": name,
                "state": record.state,
                "healthy": record.state in _HEALTHY_STATES,
                "started_at": _iso(record.started_at),
                "last_heartbeat_at": _iso(record.last_heartbeat_at),
                "last_heartbeat_age_seconds": age,
                "last_success_at": _iso(record.last_success_at),
                "last_outcome_at": _iso(record.last_outcome_at),
                "last_error_at": _iso(record.last_error_at),
                "last_error": record.last_error,
                "tick_count": record.tick_count,
                "outcome_count": record.outcome_count,
                "error_count": record.error_count,
                "crash_count": record.crash_count,
            }
        return rows


def daemon_health_status() -> str:
    snapshot = daemon_liveness_snapshot()
    if any(not row["healthy"] for row in snapshot.values()):
        return "degraded"
    return "ok"


def reset_daemon_liveness() -> None:
    with _LOCK:
        _DAEMONS.clear()
