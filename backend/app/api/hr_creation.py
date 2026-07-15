"""Authenticated user decisions for canonical HR creation drafts."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.user import User
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
    manual_steps: list[str] = Field(default_factory=list)
    confirmed_by_user_id: str | None = None
    confirmed_at: str | None = None
    created_agent_id: str | None = None
    provisioning_task_id: str | None = None
    provisioning: dict[str, Any] = Field(default_factory=dict)
    provisioning_steps: list[dict[str, Any]] = Field(default_factory=list)
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
    agent = await _load_hr_agent_for_user(db=db, current_user=current_user, agent_id=agent_id)
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


async def _load_hr_agent_for_user(*, db: AsyncSession, current_user: User, agent_id: uuid.UUID):
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if agent.agent_class != "internal_system" or agent.name != "__system_hr__":
        raise HTTPException(status_code=404, detail="HR creation draft not found")
    return agent


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

    agent = await _load_hr_agent_for_user(db=db, current_user=current_user, agent_id=agent_id)
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


@router.get("/{agent_id}/hr-creation-drafts", response_model=list[HrCreationDraftOut])
async def list_hr_creation_drafts(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
):
    agent = await _load_hr_agent_for_user(db=db, current_user=current_user, agent_id=agent_id)
    drafts = list(
        (
            await db.execute(
                select(HrCreationDraft)
                .options(selectinload(HrCreationDraft.provisioning_steps))
                .where(
                    HrCreationDraft.tenant_id == agent.tenant_id,
                    HrCreationDraft.hr_agent_id == agent_id,
                    HrCreationDraft.requested_by_user_id == current_user.id,
                    HrCreationDraft.status.in_(
                        ("awaiting_confirmation", "confirmed", "creating", "provisioning", "failed")
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
    return [
        _out(hr_creation_draft_payload(draft, runtime_task=tasks_by_id.get(draft.provisioning_task_id)))
        for draft in drafts
    ]


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
    return _out(hr_creation_draft_payload(draft, runtime_task=task))


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

        created_task = build_hr_provisioning_runtime_task(draft)
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
        assert task is not None
        if draft.status not in {"failed", "provisioning"}:
            raise HrCreationConflict("invalid_status", f"HR provisioning cannot be retried from {draft.status}.")
        if draft.confirmed_by_user_id is None or draft.confirmed_at is None:
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
    try:
        await abandon_hr_creation(db, draft, actor_id=current_user.id, task=task)
    except HrCreationConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message}) from exc
    await db.commit()
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
