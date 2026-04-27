"""Approval helpers for proposed autonomous objectives."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def apply_objective_approval(
    objective: Any,
    *,
    actor_id: Any,
    decision: str,
    reason: str | None = None,
) -> bool:
    metadata = dict(getattr(objective, "metadata_json", None) or {})
    decided_at = datetime.now(timezone.utc).isoformat()
    if decision == "approved":
        objective.status = "active"
        objective.blocked_reason = None
        metadata["requires_approval"] = False
        metadata["approval"] = {
            "actor_id": str(actor_id),
            "decision": "approved",
            "decided_at": decided_at,
            "reason": reason,
        }
        objective.metadata_json = metadata
        return True
    if decision == "rejected":
        objective.status = "rejected"
        metadata["requires_approval"] = False
        metadata["rejection"] = {
            "actor_id": str(actor_id),
            "decision": "rejected",
            "decided_at": decided_at,
            "reason": reason,
        }
        objective.metadata_json = metadata
        return True
    return False
