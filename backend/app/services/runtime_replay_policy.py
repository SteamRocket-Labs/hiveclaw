from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID


RuntimeReplayAction = Literal["ignore_live_claim", "requeue", "needs_reconciliation"]
RUNTIME_RESTART_REPLAY_CONTRACT_SCHEMA = "runtime_restart_replay_contract.v1"


@dataclass(frozen=True, slots=True)
class RuntimeReplaySnapshot:
    """Pure input used by every RuntimeTask restart/reclaim boundary."""

    task_id: UUID
    task_type: str
    status: str
    claim_version: int
    claimed_by: str | None
    claim_expires_at: datetime | None
    child_session_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeReplayDisposition:
    action: RuntimeReplayAction
    reason: str


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_deterministic_workflow_trigger(metadata: dict[str, Any]) -> bool:
    protocol = metadata.get("workflow_batch_protocol")
    if not isinstance(protocol, dict) or protocol.get("mode") != "deterministic_workflow_ref":
        return False
    raw_trigger_ids = metadata.get("trigger_ids")
    protocol_trigger_ids = protocol.get("trigger_ids")
    if not isinstance(raw_trigger_ids, list) or not isinstance(protocol_trigger_ids, list):
        return False
    try:
        trigger_ids = [UUID(str(item)) for item in raw_trigger_ids]
        protocol_ids = [UUID(str(item)) for item in protocol_trigger_ids]
    except (TypeError, ValueError, AttributeError):
        return False
    return bool(trigger_ids) and len(trigger_ids) == len(set(trigger_ids)) and set(trigger_ids) == set(protocol_ids)


def has_runtime_restart_replay_contract(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: UUID | str,
) -> bool:
    contract = (metadata or {}).get("restart_replay_contract")
    normalized_task_id = str(task_id)
    if isinstance(task_id, UUID):
        normalized_task_id = task_id.hex
    if not isinstance(contract, dict):
        return False
    return bool(
        contract.get("schema") == RUNTIME_RESTART_REPLAY_CONTRACT_SCHEMA
        and str(contract.get("task_type") or "") == str(task_type or "")
        and str(contract.get("task_id") or "") in {normalized_task_id, str(task_id)}
        and str(contract.get("idempotency_key") or "")
        in {
            f"{task_type}:{normalized_task_id}:restart",
            f"{task_type}:{task_id}:restart",
        }
        and contract.get("mode") == "durable_restart_replay"
        and contract.get("requires_completion_journal") is True
    )


def runtime_replay_disposition(
    snapshot: RuntimeReplaySnapshot,
    *,
    now: datetime | None = None,
) -> RuntimeReplayDisposition:
    """Decide replay safety without performing IO or mutating RuntimeTask state."""

    current = _aware_utc(now) or datetime.now(timezone.utc)
    claim_expires_at = _aware_utc(snapshot.claim_expires_at)
    if snapshot.status == "running" and claim_expires_at is not None and claim_expires_at > current:
        return RuntimeReplayDisposition("ignore_live_claim", "live_claim_owned_by_worker")

    metadata = snapshot.metadata
    explicit_session_bound = metadata.get("session_bound") is True
    audit_session_bound = snapshot.task_type in {"heartbeat", "trigger", "subagent", "delegation"} and bool(
        str(snapshot.child_session_id or "").strip()
    )
    session_bound = explicit_session_bound or audit_session_bound

    if snapshot.status in {"pending", "resumable"}:
        return RuntimeReplayDisposition("requeue", "unclaimed_runtime_ready")

    if snapshot.status != "running":
        return RuntimeReplayDisposition("needs_reconciliation", "runtime_status_not_replayable")

    if snapshot.task_type == "trigger" and _stable_deterministic_workflow_trigger(metadata):
        if has_runtime_restart_replay_contract(
            metadata,
            task_type=snapshot.task_type,
            task_id=snapshot.task_id,
        ):
            return RuntimeReplayDisposition("requeue", "stable_deterministic_workflow_trigger")
        return RuntimeReplayDisposition("needs_reconciliation", "invalid_restart_replay_contract")

    side_effect_risk = str(metadata.get("side_effect_risk") or "").strip().lower()
    if snapshot.task_type == "heartbeat" and not session_bound and side_effect_risk == "internal_governed":
        if has_runtime_restart_replay_contract(
            metadata,
            task_type=snapshot.task_type,
            task_id=snapshot.task_id,
        ):
            return RuntimeReplayDisposition("requeue", "unstarted_governed_heartbeat")
        return RuntimeReplayDisposition("needs_reconciliation", "invalid_restart_replay_contract")

    if session_bound or side_effect_risk not in {"", "read_only", "internal_safe"}:
        return RuntimeReplayDisposition("needs_reconciliation", "expired_session_bound_or_mutating_runtime")

    if not has_runtime_restart_replay_contract(
        metadata,
        task_type=snapshot.task_type,
        task_id=snapshot.task_id,
    ):
        return RuntimeReplayDisposition("needs_reconciliation", "invalid_restart_replay_contract")
    return RuntimeReplayDisposition("requeue", "expired_replay_safe_runtime")


def runtime_replay_snapshot_from_record(record: dict[str, Any]) -> RuntimeReplaySnapshot:
    claim_expires_at = record.get("claim_expires_at")
    if claim_expires_at is not None and not isinstance(claim_expires_at, datetime):
        try:
            claim_expires_at = datetime.fromisoformat(str(claim_expires_at).replace("Z", "+00:00"))
        except ValueError:
            claim_expires_at = None
    return RuntimeReplaySnapshot(
        task_id=UUID(str(record.get("task_id") or record.get("id"))),
        task_type=str(record.get("task_type") or ""),
        status=str(record.get("status") or ""),
        claim_version=int(record.get("claim_version") or 0),
        claimed_by=str(record.get("claimed_by") or "").strip() or None,
        claim_expires_at=claim_expires_at,
        child_session_id=str(record.get("child_session_id") or "").strip() or None,
        metadata=dict(record.get("metadata") or record.get("metadata_json") or {}),
    )


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


async def apply_runtime_replay_reconciliation(
    db: Any,
    runtime_task: Any,
    *,
    reason: str,
    summary: str | None = None,
    metadata_json: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Persist the policy's terminal reconciliation in the caller transaction."""

    detected_at = _aware_utc(now) or datetime.now(timezone.utc)
    metadata = dict(getattr(runtime_task, "metadata_json", None) or {})
    metadata.update(metadata_json or {})
    previous_claim = {
        "worker_id": str(getattr(runtime_task, "claimed_by", None) or ""),
        "claim_version": int(getattr(runtime_task, "claim_version", 0) or 0),
        "claim_expires_at": (
            runtime_task.claim_expires_at.isoformat() if getattr(runtime_task, "claim_expires_at", None) else None
        ),
    }
    frame_id = f"runtime-replay:{runtime_task.id}:{previous_claim['claim_version']}"
    frames = [dict(item) for item in metadata.get("recovery_tool_frames", []) if isinstance(item, dict)]
    if not any(str(item.get("tool_call_id") or "") == frame_id for item in frames):
        frames.append(
            {
                "runtime_task_id": str(runtime_task.id),
                "tool_call_id": frame_id,
                "tool_name": f"{runtime_task.task_type}_runtime_replay",
                "status": "needs_reconciliation",
                "event_type": "expired_claim_outcome_unknown",
                "reason": reason,
            }
        )
    metadata.update(
        {
            "needs_reconciliation": True,
            "reconciliation_status": "open",
            "reconciliation_reason": reason,
            "reconciliation_detected_at": detected_at.isoformat(),
            "previous_claim": previous_claim,
            "recovery_tool_frames": frames,
            "recovery_evidence_status": "ready",
        }
    )
    runtime_task.status = "needs_reconciliation"
    runtime_task.result_summary = (
        summary or "Expired runtime claim has unknown side effects and requires reconciliation."
    )
    runtime_task.completed_at = detected_at
    runtime_task.claimed_by = None
    runtime_task.claim_expires_at = None
    runtime_task.claim_version = previous_claim["claim_version"] + 1
    runtime_task.metadata_json = metadata

    tenant_id = _uuid_or_none(getattr(runtime_task, "tenant_id", None))
    parent_session_id = _uuid_or_none(getattr(runtime_task, "parent_session_id", None))
    parent_agent_id = _uuid_or_none(getattr(runtime_task, "parent_agent_id", None))
    parent_user_id = _uuid_or_none(getattr(runtime_task, "root_user_id", None))
    if None in (tenant_id, parent_session_id, parent_agent_id, parent_user_id):
        incomplete = set(metadata.get("recovery_evidence_incomplete_reasons") or [])
        incomplete.add("runtime_replay_notification_authority_missing")
        metadata["recovery_evidence_incomplete_reasons"] = sorted(incomplete)
        metadata["recovery_evidence_status"] = "incomplete"
        runtime_task.metadata_json = metadata
        return

    from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification

    outbox_id = await enqueue_completion_notification(
        db,
        CompletionNotification(
            tenant_id=tenant_id,
            source_kind=str(runtime_task.task_type),
            source_run_id=str(runtime_task.id),
            parent_session_id=parent_session_id,
            parent_agent_id=parent_agent_id,
            parent_user_id=parent_user_id,
            terminal_status="needs_reconciliation",
            task_type=str(runtime_task.task_type),
            summary=str(runtime_task.result_summary),
            child_session_id=_uuid_or_none(getattr(runtime_task, "child_session_id", None)),
            child_agent_name=getattr(runtime_task, "child_agent_name", None),
            metadata={"reconciliation_reason": reason, "previous_claim": previous_claim},
            payload_rank=150,
        ),
    )
    metadata["completion_outbox_id"] = str(outbox_id)
    runtime_task.metadata_json = metadata
