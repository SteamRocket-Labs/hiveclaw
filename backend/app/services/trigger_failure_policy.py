"""Trigger-failure backoff policy for autonomous trigger reliability.

Pure helpers that mutate a trigger's ``config`` to record/clear an
exponential failure backoff. No objective concept — a schedule is just a
trigger, and a failing trigger simply waits before its next attempt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.runtime.failure_policy import build_runtime_failure_policy

DEFAULT_FAILURE_BACKOFF_SECONDS = 60
MAX_FAILURE_BACKOFF_SECONDS = 60 * 60


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def apply_trigger_failure_policy(trigger: Any, *, error: str, now: datetime | None = None) -> dict[str, Any]:
    current = _now(now)
    config = dict(getattr(trigger, "config", None) or {})
    failure_count = int(config.get("failure_count") or 0) + 1
    backoff_seconds = min(
        DEFAULT_FAILURE_BACKOFF_SECONDS * (2 ** max(failure_count - 1, 0)), MAX_FAILURE_BACKOFF_SECONDS
    )
    backoff_until = current + timedelta(seconds=backoff_seconds)
    runtime_failure_policy = build_runtime_failure_policy(
        failure_kind="trigger_preflight_skip",
        message=str(error),
        retryable=True,
        safe_to_continue=True,
    )
    config.update(
        {
            "failure_count": failure_count,
            "last_failure_at": current.isoformat(),
            "last_failure": str(error)[:1000],
            "backoff_until": backoff_until.isoformat(),
            "last_runtime_failure_policy": runtime_failure_policy,
        }
    )
    trigger.config = config
    return {
        "failure_count": failure_count,
        "backoff_seconds": backoff_seconds,
        "backoff_until": backoff_until.isoformat(),
        "runtime_failure_policy": runtime_failure_policy,
    }


def reset_trigger_failure_policy(trigger: Any) -> bool:
    config = dict(getattr(trigger, "config", None) or {})
    changed = False
    for key in ("failure_count", "last_failure_at", "last_failure", "backoff_until"):
        if key in config:
            config.pop(key, None)
            changed = True
    if changed:
        trigger.config = config
    return changed
