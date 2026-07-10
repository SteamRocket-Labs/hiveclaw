"""Pure RuntimeTask transitions for explicit Workflow user controls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

WorkflowResumeRequestKind = Literal["repair", "gate_approved", "gate_rejected"]

_ALLOWED_STATUSES: dict[WorkflowResumeRequestKind, frozenset[str]] = {
    "repair": frozenset({"failed", "suspended", "killed"}),
    "gate_approved": frozenset({"suspended"}),
    "gate_rejected": frozenset({"suspended"}),
}


def queue_workflow_resume_record(
    task: Any,
    *,
    request_kind: WorkflowResumeRequestKind,
    actor_user_id: UUID | str,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Any:
    """Re-open the same durable Workflow task for the shared runtime worker.

    Gate decisions and repair are deliberately different request kinds. Only
    an explicit repair increments repair telemetry; approving a pending gate
    simply makes the already-journaled run eligible for another worker claim.
    """

    if str(getattr(task, "task_type", "") or "") != "workflow":
        raise ValueError("workflow RuntimeTask is required")
    if request_kind not in _ALLOWED_STATUSES:
        raise ValueError(f"unsupported workflow resume request: {request_kind}")
    current_status = str(getattr(task, "status", "") or "").strip().lower()
    if current_status not in _ALLOWED_STATUSES[request_kind]:
        raise ValueError(f"workflow in status {current_status!r} cannot be queued for {request_kind}")

    current = now or datetime.now(UTC)
    metadata = dict(getattr(task, "metadata_json", None) or {})
    dynamic = dict(metadata.get("dynamic_workflow") or {})
    if request_kind == "repair":
        dynamic["repair_attempts"] = int(dynamic.get("repair_attempts") or 0) + 1
        metadata["dynamic_workflow"] = dynamic

    request = {
        "schema": "hive.ccplus.workflow_resume_request.v1",
        "kind": request_kind,
        "actor_user_id": str(actor_user_id),
        "requested_at": current.isoformat(),
        **dict(details or {}),
    }
    metadata["workflow_resume_request"] = request
    task.metadata_json = metadata
    task.status = "pending"
    task.completed_at = None
    task.scheduled_at = None
    task.claimed_by = None
    task.claim_expires_at = None
    return task
