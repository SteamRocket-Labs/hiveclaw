"""User-directed recovery actions for unfinished HR-created employees."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.services.hr_creation_service import HrCreationConflict

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.user import User


_ABANDONABLE_STATUSES = frozenset({"awaiting_confirmation", "confirmed", "creating", "provisioning", "failed"})


def _fence_abandoned_task(
    task: RuntimeTask,
    *,
    actor_id: uuid.UUID,
    now: datetime,
    uncertain: bool,
) -> None:
    task.status = "needs_reconciliation" if uncertain else "killed"
    task.result_summary = (
        "HR creation was abandoned after side effects may have started."
        if uncertain
        else "HR creation was abandoned by its requester."
    )
    task.claim_version = int(task.claim_version or 0) + (1 if uncertain else 0)
    task.claimed_by = None
    task.claim_expires_at = None
    task.completed_at = now
    task.metadata_json = {
        **dict(task.metadata_json or {}),
        "phase": "terminal",
        "abandoned_at": now.isoformat(),
        "abandoned_by_user_id": str(actor_id),
        "needs_reconciliation": uncertain,
        "automatic_retry_allowed": False,
        "outcome": {"status": task.status},
    }


async def _retire_unfinished_employee(
    db: AsyncSession,
    draft: HrCreationDraft,
    *,
    actor: "User",
) -> None:
    """Deactivate the partial employee through the same path as 停用.

    G1: a digital employee is never deleted, and an abandoned HR creation can
    already own conversations and remote work once its Agent row exists, so
    the retirement is a deactivation — ``agent_retirement.prepare`` commits
    ``deactivated_at`` (status gates closed), recalls outward work, and the
    native runtime settlement withdraws work still running on its own turns.
    Nothing writes ``deleted_at`` and no file is ever moved.
    """

    if draft.created_agent_id is None:
        return
    employee = (
        await db.execute(
            select(Agent)
            .where(Agent.id == draft.created_agent_id, Agent.tenant_id == draft.tenant_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if employee is None or employee.deactivated_at is not None:
        return

    from app.services.agent_retirement import prepare

    await prepare(db, agent=employee, actor=actor)

    from app.services.agent_identity_lifecycle import terminate_agent_runtime_tasks

    await terminate_agent_runtime_tasks(
        db,
        employee,
        actor_id=actor.id,
        reason="hr_creation_abandoned",
        terminal_source="hr_creation_recovery:abandon_hr_creation",
    )

    from app.services.ai_asset_adapters import project_agent
    from app.services.ai_assets import register_projection

    await register_projection(
        db,
        project_agent(employee),
        change_source="revoke",
        actor_user_id=actor.id,
        change_message="Unfinished HR-created employee abandoned (deactivated)",
    )


async def abandon_hr_creation(
    db: AsyncSession,
    draft: HrCreationDraft,
    *,
    actor: "User",
    task: RuntimeTask | None,
) -> RuntimeTask | None:
    """Fence execution, deactivate any partial Agent, and preserve audit evidence.

    Returns the fenced task. The employee retirement is a deactivation
    (never a deletion, never a file move), so nothing is owed after the
    caller commits; a repeat abandon of an already-superseded draft is the
    ordinary invalid_status conflict.
    """

    if draft.status == "superseded":
        raise HrCreationConflict("invalid_status", f"HR draft cannot be abandoned from {draft.status}.")
    if draft.status not in _ABANDONABLE_STATUSES:
        raise HrCreationConflict("invalid_status", f"HR draft cannot be abandoned from {draft.status}.")
    now = datetime.now(timezone.utc)
    uncertain = bool(draft.claim_token) or (task is not None and task.status in {"running", "needs_reconciliation"})
    if task is not None:
        _fence_abandoned_task(task, actor_id=actor.id, now=now, uncertain=uncertain)
    await _retire_unfinished_employee(db, draft, actor=actor)

    draft.status = "superseded"
    draft.claim_token = None
    draft.claim_heartbeat_at = None
    draft.claim_expires_at = None
    draft.failure_code = "abandoned_by_requester"
    draft.failure_message = "The unfinished digital employee was removed by its requester."
    draft.provisioning_json = {
        **dict(draft.provisioning_json or {}),
        "abandoned_at": now.isoformat(),
        "abandoned_by_user_id": str(actor.id),
        "runtime_status": task.status if task is not None else None,
    }
    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="hr.creation_abandoned",
        severity="warning" if uncertain else "info",
        actor_type="user",
        actor_id=actor.id,
        tenant_id=draft.tenant_id,
        action="abandon_hr_creation",
        resource_type="hr_creation_draft",
        resource_id=draft.id,
        details={
            "created_agent_id": str(draft.created_agent_id) if draft.created_agent_id else None,
            "provisioning_task_id": str(draft.provisioning_task_id) if draft.provisioning_task_id else None,
            "needs_reconciliation": uncertain,
        },
    )
    return task
