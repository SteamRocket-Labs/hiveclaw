"""Low-cardinality observability for the RecoveryManifest authority boundary."""

from __future__ import annotations

from collections import Counter
from threading import Lock


_LOCK = Lock()
_EVENTS: Counter[tuple[str, str, str]] = Counter()
_OPERATIONS = frozenset({"resolve", "load", "persist", "resource"})
_STATUSES = frozenset(
    {
        "bound",
        "loaded",
        "loaded_migrated",
        "absent",
        "held",
        "quarantined",
        "unavailable",
        "written",
        "deleted",
        "skipped",
    }
)
_REASONS = frozenset(
    {
        "none",
        "tenant_id_unavailable",
        "agent_id_unavailable",
        "session_id_drift",
        "session_id_unavailable",
        "execution_principal_tenant_mismatch",
        "requester_principal_mismatch",
        "principal_unavailable",
        "a2a_policy_snapshot_missing",
        "a2a_policy_snapshot_drift",
        "authority_unavailable",
        "session_context_unavailable",
        "root_runtime_task_id_unavailable",
        "different_root_runtime_task",
        "tenant_id_mismatch",
        "agent_id_mismatch",
        "requester_user_id_mismatch",
        "session_id_mismatch",
        "root_session_id_mismatch",
        "root_runtime_task_id_mismatch",
        "principal_type_mismatch",
        "principal_id_mismatch",
        "principal_snapshot_hash_mismatch",
        "policy_snapshot_hash_mismatch",
        "config_snapshot_hash_mismatch",
        "base_transcript_sequence_mismatch",
        "delegation_authority_hash_mismatch",
        "corrupt_json",
        "legacy_authority_unverifiable",
        "integrity_mismatch",
        "unsupported_envelope_schema",
        "invalid_envelope_shape",
        "legacy_authority_mismatch",
        "legacy_migrated",
        "policy_snapshot_changed_before_persist",
        "base_transcript_sequence_changed_before_persist",
        "empty_manifest",
        "invalid_resource_ref",
        "resource_authority_incomplete",
        "resource_path_rejected",
        "resource_not_found",
        "resource_read_unavailable",
        "resource_hash_mismatch",
        "resource_authority_digest_mismatch",
        "resource_snapshot_unavailable",
        "checkpoint_persist_unavailable",
    }
)


def _bounded(value: str | None, allowed: frozenset[str], *, empty: str) -> str:
    normalized = str(value or empty).strip().lower()
    return normalized if normalized in allowed else "other"


def record_recovery_manifest_event(*, operation: str, status: str, reason: str | None = None) -> None:
    key = (
        _bounded(operation, _OPERATIONS, empty="other"),
        _bounded(status, _STATUSES, empty="other"),
        _bounded(reason, _REASONS, empty="none"),
    )
    with _LOCK:
        _EVENTS[key] += 1


def reset_recovery_manifest_metrics() -> None:
    with _LOCK:
        _EVENTS.clear()


def snapshot_recovery_manifest_metrics() -> dict[str, int]:
    with _LOCK:
        return {":".join(key): value for key, value in sorted(_EVENTS.items())}


def _labels(*, operation: str, status: str, reason: str) -> str:
    return f'{{operation="{operation}",status="{status}",reason="{reason}"}}'


def render_recovery_manifest_prometheus() -> str:
    with _LOCK:
        lines = [
            "# HELP recovery_manifest_events_total Recovery authority resolution, load, and persist outcomes.",
            "# TYPE recovery_manifest_events_total counter",
        ]
        for (operation, status, reason), count in sorted(_EVENTS.items()):
            lines.append(
                f"recovery_manifest_events_total{_labels(operation=operation, status=status, reason=reason)} {count}"
            )
        return "\n".join(lines) + "\n"


__all__ = [
    "record_recovery_manifest_event",
    "render_recovery_manifest_prometheus",
    "reset_recovery_manifest_metrics",
    "snapshot_recovery_manifest_metrics",
]
