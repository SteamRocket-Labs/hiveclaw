"""Authenticated user decisions for canonical HR creation drafts."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import get_current_user
from app.database import get_db
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.hr_creation_source import load_creation_source_agent, require_creation_capability
from app.services.hr_creation_service import (
    HrCreationConflict,
    confirm_hr_creation_draft_record,
    hr_creation_draft_payload,
    load_hr_creation_draft,
    reject_hr_creation_draft_record,
)

router = APIRouter(prefix="/agents", tags=["hr-creation"])


class HrCreationConfirmIn(BaseModel):
    blueprint_version: int = Field(ge=1)
    blueprint_hash: str | None = Field(default=None, min_length=8, max_length=80)


class HrCreationDraftOut(BaseModel):
    blueprint_id: str
    blueprint_version: int
    blueprint_hash: str
    draft_status: str
    blueprint: dict[str, Any]
    summary: dict[str, Any] = Field(default_factory=dict)
    ready_now: list[str] = Field(default_factory=list)
    will_install: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    creation_authorization: dict[str, Any] | None = None
    manual_steps: list[str] = Field(default_factory=list)
    confirmed_by_user_id: str | None = None
    confirmed_at: str | None = None
    created_agent_id: str | None = None
    provisioning_task_id: str | None = None
    provisioning: dict[str, Any] = Field(default_factory=dict)
    provisioning_steps: list[dict[str, Any]] = Field(default_factory=list)
    first_task_work: dict[str, Any] | None = None
    creation_state: str | None = None
    failure: dict[str, Any] | None = None
    hr_agent_id: str | None = None
    session_id: str | None = None
    requested_by_user_id: str | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    recovery: dict[str, Any] = Field(default_factory=dict)


def _out(payload: dict[str, Any]) -> HrCreationDraftOut:
    return HrCreationDraftOut.model_validate(payload)


async def _load_for_user(
    *,
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    for_update: bool,
):
    agent = await _load_hr_agent_for_user(
        db=db, current_user=current_user, agent_id=agent_id, require_execute=for_update,
    )
    try:
        return await load_hr_creation_draft(
            db,
            draft_id=draft_id,
            tenant_id=agent.tenant_id,
            hr_agent_id=agent_id,
            requested_by_user_id=current_user.id,
            for_update=for_update,
        )
    except HrCreationConflict as exc:
        status_code = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status_code, detail={"error": exc.code, "message": exc.message}) from exc


async def _load_hr_agent_for_user(
    *, db: AsyncSession, current_user: User, agent_id: uuid.UUID, require_execute: bool = False,
):
    return await load_creation_source_agent(db, current_user, agent_id, require_execute=require_execute)


async def _linked_provisioning_task_or_none(
    db: AsyncSession,
    *,
    draft: HrCreationDraft,
    for_update: bool = False,
):
    if draft.provisioning_task_id is None:
        return None
    statement = select(RuntimeTask).where(
        RuntimeTask.id == draft.provisioning_task_id,
        RuntimeTask.tenant_id == draft.tenant_id,
        RuntimeTask.task_type == "hr_provisioning",
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def _load_hr_draft_and_task_for_mutation(
    *,
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    task_required: bool = True,
) -> tuple[HrCreationDraft, RuntimeTask | None]:
    """Lock one HR mutation in the global RuntimeTask -> draft order."""

    agent = await _load_hr_agent_for_user(
        db=db, current_user=current_user, agent_id=agent_id, require_execute=True,
    )
    try:
        candidate = await load_hr_creation_draft(
            db,
            draft_id=draft_id,
            tenant_id=agent.tenant_id,
            hr_agent_id=agent_id,
            requested_by_user_id=current_user.id,
            for_update=False,
        )
        linked_task_id = candidate.provisioning_task_id
        task: RuntimeTask | None = None
        if linked_task_id is not None:
            task = (
                await db.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.id == linked_task_id,
                        RuntimeTask.tenant_id == agent.tenant_id,
                        RuntimeTask.task_type == "hr_provisioning",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None:
                raise HrCreationConflict("provisioning_task_missing", "HR provisioning task link is invalid.")
        elif task_required:
            raise HrCreationConflict(
                "provisioning_task_missing",
                "Confirmed HR draft has no durable provisioning task.",
            )
        draft = await load_hr_creation_draft(
            db,
            draft_id=draft_id,
            tenant_id=agent.tenant_id,
            hr_agent_id=agent_id,
            requested_by_user_id=current_user.id,
            for_update=True,
        )
        if draft.provisioning_task_id != linked_task_id:
            raise HrCreationConflict(
                "retry_required",
                "HR provisioning state changed while acquiring mutation authority; reload and retry.",
            )
        return draft, task
    except HrCreationConflict as exc:
        status_code = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status_code, detail={"error": exc.code, "message": exc.message}) from exc


@router.get("/hr-creation-drafts/mine", response_model=list[HrCreationDraftOut])
async def list_my_hr_creation_drafts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
):
    from app.core.tenant_scope import resolve_and_pin_tenant_scope

    tenant_id = await resolve_and_pin_tenant_scope(db, current_user, None)
    return await _list_creation_drafts(db, current_user, tenant_id=tenant_id, agent_id=None, limit=limit)


@router.get("/{agent_id}/hr-creation-drafts", response_model=list[HrCreationDraftOut])
async def list_hr_creation_drafts(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
):
    agent = await _load_hr_agent_for_user(db=db, current_user=current_user, agent_id=agent_id)
    return await _list_creation_drafts(db, current_user, tenant_id=agent.tenant_id, agent_id=agent_id, limit=limit)


async def _list_creation_drafts(db, current_user, *, tenant_id, agent_id, limit):
    scope = [HrCreationDraft.tenant_id == tenant_id, HrCreationDraft.requested_by_user_id == current_user.id]
    if agent_id is not None:
        scope.append(HrCreationDraft.hr_agent_id == agent_id)
    drafts = list(
        (
            await db.execute(
                select(HrCreationDraft)
                .options(selectinload(HrCreationDraft.provisioning_steps))
                .where(
                    *scope,
                    or_(
                        HrCreationDraft.status.in_(("awaiting_confirmation", "confirmed", "creating", "provisioning", "failed")),
                        and_(HrCreationDraft.status == "completed",
                             HrCreationDraft.provisioning_json["first_task"]["status"].astext.in_(("blocked", "unknown", "needs_reconciliation"))),
                    ),
                )
                .order_by(
                    HrCreationDraft.updated_at.desc(),
                    HrCreationDraft.created_at.desc(),
                )
                .limit(max(1, min(int(limit), 100)))
            )
        )
        .scalars()
        .all()
    )
    task_ids = [draft.provisioning_task_id for draft in drafts if draft.provisioning_task_id is not None]
    tasks_by_id = {}
    if task_ids:
        tasks = list((await db.execute(select(RuntimeTask).where(RuntimeTask.id.in_(task_ids)))).scalars().all())
        tasks_by_id = {task.id: task for task in tasks}
    output = [
        _out(hr_creation_draft_payload(draft, runtime_task=tasks_by_id.get(draft.provisioning_task_id)))
        for draft in drafts
    ]
    from app.models.agent import Agent
    from app.services.agent_manager import agent_manager

    # A commit followed by a crash/failed archive must remain discoverable
    # after reload. Existing abandoned drafts + deleted rows + live files
    # are the durable truth; a successful archive needs no separate marker.
    cleanup_drafts = (
        (
            await db.execute(
                select(HrCreationDraft)
                .options(selectinload(HrCreationDraft.provisioning_steps))
                .join(Agent, Agent.id == HrCreationDraft.created_agent_id)
                .where(
                    *scope,
                    HrCreationDraft.status == "superseded",
                    HrCreationDraft.failure_code == "abandoned_by_requester",
                    Agent.tenant_id == tenant_id,
                    Agent.deleted_at.is_not(None),
                )
                .order_by(HrCreationDraft.updated_at, HrCreationDraft.id)
            )
        )
        .scalars()
        .all()
    )
    pending = []
    for draft in cleanup_drafts:
        if not agent_manager.has_agent_files(draft.created_agent_id):
            continue
        payload = hr_creation_draft_payload(draft)
        payload["recovery"].update(cleanup_pending=True, can_abandon=True)
        pending.append(_out(payload))
        if len(pending) >= max(1, min(int(limit), 100)):
            break
    combined = [*pending, *output][: max(1, min(int(limit), 100))]
    if agent_id is not None:
        return combined
    visible = []
    for payload in combined:
        try:
            await load_creation_source_agent(db, current_user, uuid.UUID(payload.hr_agent_id))
        except HTTPException as exc:
            if exc.status_code not in {403, 404, 410}:
                raise
            continue
        visible.append(payload)
    return visible


@router.get("/{agent_id}/hr-creation-drafts/{draft_id}", response_model=HrCreationDraftOut)
async def get_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft = await _load_for_user(
        db=db, current_user=current_user, agent_id=agent_id, draft_id=draft_id, for_update=False
    )
    task = await _linked_provisioning_task_or_none(db, draft=draft)
    from app.services.employee_first_task import load_first_task_work

    return _out({**hr_creation_draft_payload(draft, runtime_task=task),
        "first_task_work": await load_first_task_work(db, draft=draft, user=current_user)})


@router.post("/{agent_id}/hr-creation-drafts/{draft_id}/confirm", response_model=HrCreationDraftOut)
async def confirm_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    payload: HrCreationConfirmIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft = await _load_for_user(
        db=db, current_user=current_user, agent_id=agent_id, draft_id=draft_id, for_update=True
    )
    was_awaiting_confirmation = draft.status == "awaiting_confirmation"
    if was_awaiting_confirmation or (draft.status != "completed" and draft.provisioning_task_id is None):
        await require_creation_capability(db, draft.tenant_id, draft.hr_agent_id)
    try:
        confirm_hr_creation_draft_record(
            draft,
            confirming_user_id=current_user.id,
            blueprint_version=payload.blueprint_version,
            blueprint_hash=payload.blueprint_hash,
        )
    except HrCreationConflict as exc:
        error = "stale_confirmation" if exc.code == "version_mismatch" else exc.code
        raise HTTPException(
            status_code=409,
            detail={
                "error": error,
                "reason_code": exc.code,
                "message": exc.message,
                "current": _out(hr_creation_draft_payload(draft)).model_dump(mode="json"),
            },
        ) from exc

    created_task: RuntimeTask | None = None
    if draft.status != "completed" and draft.provisioning_task_id is None:
        from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task
        from app.services.session_writer_epoch import assign_runtime_task_writer_generation

        created_task = build_hr_provisioning_runtime_task(draft)
        await assign_runtime_task_writer_generation(db, created_task)
        db.add(created_task)
        await db.flush()
        draft.provisioning_task_id = created_task.id
        draft.provisioning_json = {
            **dict(draft.provisioning_json or {}),
            "runtime_task_id": str(created_task.id),
            "runtime_status": created_task.status,
            "runtime_phase": "queued",
        }

    from app.core.policy import write_audit_event

    if was_awaiting_confirmation:
        await write_audit_event(
            db,
            event_type="hr.creation_blueprint_confirmed",
            severity="info",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=draft.tenant_id,
            action="confirm_hr_creation_blueprint",
            resource_type="hr_creation_draft",
            resource_id=draft.id,
            details={
                "blueprint_version": draft.blueprint_version,
                "blueprint_hash": draft.blueprint_hash,
                "provisioning_task_id": str(created_task.id) if created_task else None,
            },
        )
    await db.commit()
    if created_task is not None:
        from app.services.runtime_task_worker import notify_runtime_task_worker

        await notify_runtime_task_worker(
            reason="hr_provisioning_queued",
            runtime_task_id=created_task.id,
        )
    task = created_task or await _linked_provisioning_task_or_none(db, draft=draft)
    return _out(hr_creation_draft_payload(draft, runtime_task=task))


@router.post("/{agent_id}/hr-creation-drafts/{draft_id}/retry", response_model=HrCreationDraftOut)
async def retry_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft, task = await _load_hr_draft_and_task_for_mutation(
        db=db,
        current_user=current_user,
        agent_id=agent_id,
        draft_id=draft_id,
    )
    try:
        from app.services.hr_creation_service import direct_creation_authorization
        from app.services.employee_first_task import needs_first_task_dispatch

        if direct_creation_authorization(draft):
            from app.services.direct_employee_creation import validate_direct_creation

            await validate_direct_creation(db, draft, check_creation_policy=draft.status != "completed")
        else:
            await require_creation_capability(db, draft.tenant_id, draft.hr_agent_id)
        assert task is not None
        if draft.status not in {"failed", "provisioning"} and not (
            draft.status == "completed" and needs_first_task_dispatch(draft)
        ):
            raise HrCreationConflict("invalid_status", f"HR provisioning cannot be retried from {draft.status}.")
        from app.services.hr_creation_service import has_creation_authorization

        if not has_creation_authorization(draft):
            raise HrCreationConflict(
                "confirmation_missing",
                "HR provisioning cannot retry without authenticated blueprint confirmation.",
            )
        if task.status == "needs_reconciliation":
            raise HrCreationConflict(
                "reconciliation_required",
                "HR provisioning has unknown side effects and requires operator reconciliation before retry.",
            )
        if task.status not in {"failed", "killed", "resumable"}:
            raise HrCreationConflict("invalid_task_status", f"HR provisioning task cannot retry from {task.status}.")
    except HrCreationConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message}) from exc

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if draft.status != "completed":
        draft.status = "confirmed" if draft.status == "failed" else "provisioning"
    draft.failure_code = None
    draft.failure_message = None
    draft.claim_token = None
    draft.claim_heartbeat_at = None
    draft.claim_expires_at = None
    task.status = "resumable"
    task.scheduled_at = now
    task.started_at = None
    task.completed_at = None
    task.result_summary = None
    task.claimed_by = None
    task.claim_expires_at = None
    task.metadata_json = {
        **dict(task.metadata_json or {}),
        "phase": "retry_queued",
        "retry_queued_at": now.isoformat(),
        "side_effect_risk": "journaled",
        "automatic_retry_allowed": True,
        "outcome": None,
    }
    draft.provisioning_json = {
        **dict(draft.provisioning_json or {}),
        "runtime_task_id": str(task.id),
        "runtime_status": task.status,
        "runtime_phase": "retry_queued",
    }
    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="hr.creation_provisioning_retried",
        severity="info",
        actor_type="user",
        actor_id=current_user.id,
        tenant_id=draft.tenant_id,
        action="retry_hr_provisioning",
        resource_type="hr_creation_draft",
        resource_id=draft.id,
        details={"provisioning_task_id": str(task.id), "task_status": task.status},
    )
    await db.commit()
    from app.services.runtime_task_worker import notify_runtime_task_worker

    await notify_runtime_task_worker(reason="hr_provisioning_retry", runtime_task_id=task.id)
    return _out(hr_creation_draft_payload(draft, runtime_task=task))


@router.post("/{agent_id}/hr-creation-drafts/{draft_id}/cancel", response_model=HrCreationDraftOut)
async def cancel_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft, task = await _load_hr_draft_and_task_for_mutation(
        db=db,
        current_user=current_user,
        agent_id=agent_id,
        draft_id=draft_id,
    )
    try:
        assert task is not None
        if draft.status not in {"confirmed", "creating", "provisioning", "failed"}:
            raise HrCreationConflict("invalid_status", f"HR provisioning cannot be cancelled from {draft.status}.")
    except HrCreationConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message}) from exc

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    active_or_uncertain = task.status == "running" or bool(draft.claim_token)
    task.claim_version = int(task.claim_version or 0) + (1 if active_or_uncertain else 0)
    task.claimed_by = None
    task.claim_expires_at = None
    task.completed_at = now
    if active_or_uncertain:
        task.status = "needs_reconciliation"
        task.result_summary = "HR provisioning was cancelled after side effects may have started."
        draft.status = "failed"
        draft.failure_code = "cancellation_reconciliation_required"
        draft.failure_message = "Provisioning cancellation requires operator reconciliation."
        outcome_status = "needs_reconciliation"
    else:
        task.status = "killed"
        task.result_summary = "HR provisioning was cancelled before execution."
        draft.status = "rejected"
        draft.rejected_by_user_id = current_user.id
        draft.rejected_at = now
        draft.failure_code = None
        draft.failure_message = None
        outcome_status = "cancelled"
    draft.claim_token = None
    draft.claim_heartbeat_at = None
    draft.claim_expires_at = None
    task.metadata_json = {
        **dict(task.metadata_json or {}),
        "phase": "terminal",
        "terminal_at": now.isoformat(),
        "automatic_retry_allowed": False,
        "needs_reconciliation": active_or_uncertain,
        "outcome": {"status": outcome_status},
    }
    draft.provisioning_json = {
        **dict(draft.provisioning_json or {}),
        "runtime_task_id": str(task.id),
        "runtime_status": task.status,
        "runtime_phase": "terminal",
    }
    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="hr.creation_provisioning_cancelled",
        severity="warning" if active_or_uncertain else "info",
        actor_type="user",
        actor_id=current_user.id,
        tenant_id=draft.tenant_id,
        action="cancel_hr_provisioning",
        resource_type="hr_creation_draft",
        resource_id=draft.id,
        details={
            "provisioning_task_id": str(task.id),
            "task_status": task.status,
            "needs_reconciliation": active_or_uncertain,
        },
    )
    await db.commit()
    return _out(hr_creation_draft_payload(draft, runtime_task=task))


@router.delete("/{agent_id}/hr-creation-drafts/{draft_id}", response_model=HrCreationDraftOut)
async def abandon_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hr_creation_recovery import abandon_hr_creation

    draft, task = await _load_hr_draft_and_task_for_mutation(
        db=db,
        current_user=current_user,
        agent_id=agent_id,
        draft_id=draft_id,
        task_required=False,
    )
    from app.services.runtime_terminal_settlement import (
        RuntimeTaskLateAdmissionConflict,
        RuntimeTaskSessionBindingConflict,
    )

    try:
        _, cleanup_agent_ids = await abandon_hr_creation(db, draft, actor_id=current_user.id, task=task)
        await db.commit()
    except HrCreationConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message}) from exc
    except (RuntimeTaskLateAdmissionConflict, RuntimeTaskSessionBindingConflict) as exc:
        # Same typed mapping as the DELETE-agent endpoint: the abandon
        # transaction rolled back cleanly (draft intact, employee alive,
        # files untouched) and the client may retry once admission pressure
        # passes; a session-binding conflict is classified as not promised
        # by an identical retry.
        await db.rollback()
        binding_conflict = isinstance(exc, RuntimeTaskSessionBindingConflict)
        raise HTTPException(
            status_code=409,
            detail={
                "code": (
                    "hr_abandon_session_binding_conflict" if binding_conflict else "hr_abandon_late_admission_conflict"
                ),
                "message": str(exc),
                "retryable": not binding_conflict,
            },
        ) from exc
    if cleanup_agent_ids:
        # Destructive cleanup runs only after the abandon transaction is
        # durable; the durable retry truth is the soft-deleted employee row
        # plus its still existing data directory, and a repeat abandon
        # request reaches that cleanup through the superseded cleanup-only
        # branch instead of leaving an un-rollbackable archive in front of a
        # rolled-back transaction (CC6 B5, same shape as the DELETE-agent
        # endpoint).
        from app.services.agent_identity_lifecycle import perform_pending_agent_cleanup

        for cleanup_agent_id in cleanup_agent_ids:
            outcome = await perform_pending_agent_cleanup(draft.tenant_id, cleanup_agent_id)
            if not outcome["archive_ok"]:
                # The abandon itself committed; the client must see the
                # partial truth instead of a success payload. The message
                # never implies a DB rollback, and the supported retry is
                # the same abandon request (cleanup-only on repeat).
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "hr_abandon_cleanup_pending",
                        "retryable": True,
                        "message": (
                            "The HR creation draft was abandoned, but the employee's "
                            "file archival is still pending. The abandon is committed and "
                            "is NOT rolled back; retrying this abandon later performs "
                            "only the remaining cleanup."
                        ),
                    },
                )
    return _out(hr_creation_draft_payload(draft, runtime_task=task))


@router.post("/{agent_id}/hr-creation-drafts/{draft_id}/reject", response_model=HrCreationDraftOut)
async def reject_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft = await _load_for_user(
        db=db, current_user=current_user, agent_id=agent_id, draft_id=draft_id, for_update=True
    )
    try:
        reject_hr_creation_draft_record(draft, rejecting_user_id=current_user.id)
    except HrCreationConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message}) from exc
    await db.commit()
    return _out(hr_creation_draft_payload(draft))
