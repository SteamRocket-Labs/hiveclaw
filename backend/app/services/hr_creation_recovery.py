"""User-directed recovery actions for unfinished HR-created employees."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
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


async def _ensure_created_employee_retired(
    db: AsyncSession,
    draft: HrCreationDraft,
    *,
    actor: "User",
) -> None:
    """Refuse the abandon until the created employee is deactivated.

    G1: a digital employee is never deleted, and members and owners cannot
    deactivate one (plan §3.3) — so the abandon never deactivates either.
    It supersedes a draft whose employee is already deactivated (by anyone
    allowed to), or it changes nothing and answers with the governed
    recovery path: an administrator deactivates the employee on the
    digital-employees page, a member asks their company admin to. Nothing
    is deleted and no file is ever moved; the abandon transaction stays
    atomic because no retirement machinery runs inside it.
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

    from app.core.permissions import is_scoped_business_admin

    if is_scoped_business_admin(actor, resource_tenant_id=draft.tenant_id):
        message = (
            "This creation already produced a digital employee. Deactivate it on the "
            "digital-employees page, then abandon this draft again."
        )
    else:
        message = "This creation already produced a digital employee. Ask a company admin to deactivate it."
    raise HTTPException(
        status_code=409,
        detail={"code": "agent_retirement_administrator_required", "message": message},
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
    await _ensure_created_employee_retired(db, draft, actor=actor)

    # Re-lock the draft and task for this decision: the employee guard above
    # may have waited on its row, and a concurrent mutation must not be
    # fenced on stale state.
    draft = (
        await db.execute(
            select(HrCreationDraft).where(HrCreationDraft.id == draft.id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if draft.status not in _ABANDONABLE_STATUSES:
        raise HrCreationConflict("invalid_status", f"HR draft cannot be abandoned from {draft.status}.")
    if task is not None:
        task = (
            await db.execute(
                select(RuntimeTask).where(RuntimeTask.id == task.id).with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    now = datetime.now(timezone.utc)
    uncertain = bool(draft.claim_token) or (task is not None and task.status in {"running", "needs_reconciliation"})
    if task is not None:
        _fence_abandoned_task(task, actor_id=actor.id, now=now, uncertain=uncertain)

    draft.status = "superseded"
    draft.claim_token = None
    draft.claim_heartbeat_at = None
    draft.claim_expires_at = None
    draft.failure_code = "abandoned_by_requester"
    draft.failure_message = "The unfinished creation was abandoned by its requester; its employee stays until an admin deactivates it."
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
