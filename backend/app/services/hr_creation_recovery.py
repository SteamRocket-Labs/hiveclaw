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


async def _pending_abandon_cleanup_agent_ids(
    db: AsyncSession,
    draft: HrCreationDraft,
) -> list[uuid.UUID]:
    """Cleanup ids a committed abandon still owes, via exact durable linkage.

    Returns the linked employee's id only when that Agent row exists in the
    draft's tenant AND is already soft-deleted (a previous abandon retired it
    and the post-commit file archival failed or was interrupted). A live or
    missing employee owes nothing here, so this never turns an unrelated
    superseded draft into a new deletion.
    """
    if draft.failure_code != "abandoned_by_requester" or draft.created_agent_id is None:
        return []
    employee = (
        await db.execute(
            select(Agent).where(
                Agent.id == draft.created_agent_id,
                Agent.tenant_id == draft.tenant_id,
                Agent.deleted_at.is_not(None),
            )
        )
    ).scalar_one_or_none()
    return [employee.id] if employee is not None else []


async def _retire_unfinished_employee(
    db: AsyncSession,
    draft: HrCreationDraft,
    *,
    actor_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Soft-delete the partial employee; return ids owing post-commit cleanup.

    Same shape as the DELETE-agent endpoint fix (CC6 B5): the destructive
    file archival previously ran BEFORE ``soft_delete_agent`` inside the
    abandon transaction, so a typed late-admission conflict (or any
    rollback) left the Agent alive with its files already archived. The
    Agent row is locked FOR NO KEY UPDATE — enough to serialize Agent
    mutation without blocking the FOR KEY SHARE an in-flight transcript
    append needs on the agents row — and the file archival is deferred past
    the caller's commit. The durable retry truth is the soft-deleted Agent
    row plus its still existing data directory; a repeat abandon request
    reaches it through the superseded cleanup-only branch of
    ``abandon_hr_creation``.
    """

    if draft.created_agent_id is None:
        return []
    employee = (
        await db.execute(
            select(Agent)
            .where(Agent.id == draft.created_agent_id, Agent.tenant_id == draft.tenant_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if employee is None or employee.deleted_at is not None:
        return []

    from app.services.agent_identity_lifecycle import soft_delete_agent

    await soft_delete_agent(db, employee, actor_id=actor_id, reason="hr_creation_abandoned")

    from app.services.ai_asset_adapters import project_agent
    from app.services.ai_assets import register_projection

    await register_projection(
        db,
        project_agent(employee),
        change_source="revoke",
        actor_user_id=actor_id,
        change_message="Unfinished HR-created employee abandoned",
    )
    return [employee.id]


async def abandon_hr_creation(
    db: AsyncSession,
    draft: HrCreationDraft,
    *,
    actor_id: uuid.UUID,
    task: RuntimeTask | None,
) -> tuple[RuntimeTask | None, list[uuid.UUID]]:
    """Fence execution, retire any partial Agent, and preserve audit evidence.

    Returns the fenced task and the Agent ids whose destructive file cleanup
    is still owed AFTER the caller commits (see
    ``perform_pending_agent_cleanup``); the caller must run that cleanup
    once this transaction is durable.
    """

    if draft.status == "superseded":
        # Cleanup-only retry of an abandon that already committed. The
        # durable linkage (``draft.created_agent_id`` + ``draft.tenant_id``)
        # yields a cleanup id ONLY when that employee is already soft-deleted
        # — i.e. a previous abandon retired it and its post-commit file
        # archival failed or was interrupted. Fencing, retirement, and audit
        # never re-run. Any other superseded draft (one superseded by a
        # later blueprint revision, or whose employee is still live) keeps
        # the ordinary invalid_status conflict below.
        pending_cleanup = await _pending_abandon_cleanup_agent_ids(db, draft)
        if pending_cleanup:
            return None, pending_cleanup
        raise HrCreationConflict("invalid_status", f"HR draft cannot be abandoned from {draft.status}.")
    if draft.status not in _ABANDONABLE_STATUSES:
        raise HrCreationConflict("invalid_status", f"HR draft cannot be abandoned from {draft.status}.")
    now = datetime.now(timezone.utc)
    uncertain = bool(draft.claim_token) or (task is not None and task.status in {"running", "needs_reconciliation"})
    if task is not None:
        _fence_abandoned_task(task, actor_id=actor_id, now=now, uncertain=uncertain)
    cleanup_agent_ids = await _retire_unfinished_employee(db, draft, actor_id=actor_id)

    draft.status = "superseded"
    draft.claim_token = None
    draft.claim_heartbeat_at = None
    draft.claim_expires_at = None
    draft.failure_code = "abandoned_by_requester"
    draft.failure_message = "The unfinished digital employee was removed by its requester."
    draft.provisioning_json = {
        **dict(draft.provisioning_json or {}),
        "abandoned_at": now.isoformat(),
        "abandoned_by_user_id": str(actor_id),
        "runtime_status": task.status if task is not None else None,
    }
    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="hr.creation_abandoned",
        severity="warning" if uncertain else "info",
        actor_type="user",
        actor_id=actor_id,
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
    return task, cleanup_agent_ids
