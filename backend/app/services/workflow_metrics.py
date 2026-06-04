from __future__ import annotations

from collections import Counter, defaultdict
from threading import Lock

_LOCK = Lock()
_RUNS_STARTED = 0
_RUNS_FINISHED: Counter[str] = Counter()
_STEPS: Counter[str] = Counter()
_LEAF_CALLS: Counter[str] = Counter()
_RESUME_ATTEMPTS = 0
_RESUME_FINISHED: Counter[str] = Counter()
_QUOTA_DENIALS = 0
_HASH_MISMATCHES = 0
_STEP_DURATION_SUM = 0.0
_STEP_DURATION_COUNT = 0
_STEP_DURATION_BY_STATUS: defaultdict[str, float] = defaultdict(float)


def reset_workflow_metrics() -> None:
    global _RUNS_STARTED, _RESUME_ATTEMPTS, _QUOTA_DENIALS, _HASH_MISMATCHES
    global _STEP_DURATION_SUM, _STEP_DURATION_COUNT
    with _LOCK:
        _RUNS_STARTED = 0
        _RUNS_FINISHED.clear()
        _STEPS.clear()
        _LEAF_CALLS.clear()
        _RESUME_ATTEMPTS = 0
        _RESUME_FINISHED.clear()
        _QUOTA_DENIALS = 0
        _HASH_MISMATCHES = 0
        _STEP_DURATION_SUM = 0.0
        _STEP_DURATION_COUNT = 0
        _STEP_DURATION_BY_STATUS.clear()


def record_workflow_run_started() -> None:
    global _RUNS_STARTED
    with _LOCK:
        _RUNS_STARTED += 1


def record_workflow_run_finished(status: str) -> None:
    with _LOCK:
        _RUNS_FINISHED[status] += 1


def record_workflow_step(status: str) -> None:
    with _LOCK:
        _STEPS[status] += 1


def record_workflow_leaf_call(status: str) -> None:
    with _LOCK:
        _LEAF_CALLS[status] += 1


def record_workflow_resume_attempt() -> None:
    global _RESUME_ATTEMPTS
    with _LOCK:
        _RESUME_ATTEMPTS += 1


def record_workflow_resume_finished(status: str) -> None:
    with _LOCK:
        _RESUME_FINISHED[status] += 1


def record_workflow_quota_denial() -> None:
    global _QUOTA_DENIALS
    with _LOCK:
        _QUOTA_DENIALS += 1


def record_workflow_hash_mismatch() -> None:
    global _HASH_MISMATCHES
    with _LOCK:
        _HASH_MISMATCHES += 1


def observe_workflow_step_duration(status: str, seconds: float) -> None:
    global _STEP_DURATION_SUM, _STEP_DURATION_COUNT
    with _LOCK:
        _STEP_DURATION_SUM += max(0.0, float(seconds))
        _STEP_DURATION_COUNT += 1
        _STEP_DURATION_BY_STATUS[status] += max(0.0, float(seconds))


def snapshot_workflow_metrics() -> dict:
    with _LOCK:
        return {
            "runs_started_total": _RUNS_STARTED,
            "runs_finished_total": dict(_RUNS_FINISHED),
            "steps_total": dict(_STEPS),
            "leaf_calls_total": dict(_LEAF_CALLS),
            "resume_attempts_total": _RESUME_ATTEMPTS,
            "resume_finished_total": dict(_RESUME_FINISHED),
            "quota_denials_total": _QUOTA_DENIALS,
            "hash_mismatches_total": _HASH_MISMATCHES,
            "step_duration_seconds": {
                "count": _STEP_DURATION_COUNT,
                "sum": _STEP_DURATION_SUM,
                "by_status_sum": dict(_STEP_DURATION_BY_STATUS),
            },
        }
