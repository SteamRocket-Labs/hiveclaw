"""Bounded repair for HR draft, RuntimeTask, and unfinished Agent seams."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.services.hr_creation_service import HR_CREATION_PREVIEW_TTL
from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task


_ACTIVE_DRAFT_STATUSES = ("confirmed", "creating", "provisioning", "failed")
_TERMINAL_TASK_STATUSES = ("completed", "failed", "killed", "skipped", "needs_reconciliation")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clear_stale_claim(draft: HrCreationDraft) -> None:
    if draft.claim_token is not None:
        draft.claim_version = int(draft.claim_version or 0) + 1
    draft.claim_token = None
    draft.claim_heartbeat_at = None
    draft.claim_expires_at = None


def _has_stale_domain_claim(draft: HrCreationDraft, task: RuntimeTask, *, now: datetime) -> bool:
    if draft.status not in {"creating", "provisioning"} or draft.claim_token is None:
        return False
    if draft.claim_expires_at is not None and draft.claim_expires_at > now:
        return False
    if task.status in {"pending", "resumable"}:
        return True
    return task.status == "running" and (task.claim_expires_at is None or task.claim_expires_at <= now)


def _candidate_statement(statement, *, now: datetime, limit: int, tenant_id: uuid.UUID | None):
    stale_domain_claim = and_(
        HrCreationDraft.status.in_(("creating", "provisioning")),
        HrCreationDraft.claim_token.is_not(None),
        or_(HrCreationDraft.claim_expires_at.is_(None), HrCreationDraft.claim_expires_at <= now),
        or_(
            RuntimeTask.status.in_(("pending", "resumable")),
            and_(
                RuntimeTask.status == "running",
                or_(RuntimeTask.claim_expires_at.is_(None), RuntimeTask.claim_expires_at <= now),
            ),
        ),
    )
    statement = (
        statement.outerjoin(RuntimeTask, RuntimeTask.id == HrCreationDraft.provisioning_task_id)
        .where(
            or_(
                and_(
                    HrCreationDraft.status == "awaiting_confirmation",
                    or_(HrCreationDraft.expires_at.is_(None), HrCreationDraft.expires_at <= now),
                ),
                and_(
                    HrCreationDraft.status.in_(_ACTIVE_DRAFT_STATUSES),
                    HrCreationDraft.provisioning_task_id.is_(None),
                ),
                and_(
                    HrCreationDraft.status.in_(_ACTIVE_DRAFT_STATUSES),
                    or_(HrCreationDraft.confirmed_by_user_id.is_(None), HrCreationDraft.confirmed_at.is_(None)),
                ),
                and_(
                    HrCreationDraft.status.in_(("confirmed", "creating", "provisioning")),
                    RuntimeTask.status.in_(_TERMINAL_TASK_STATUSES),
                ),
                stale_domain_claim,
            )
        )
        .order_by(HrCreationDraft.updated_at.asc(), HrCreationDraft.created_at.asc())
        .limit(max(1, min(int(limit), 500)))
    )
    return statement.where(HrCreationDraft.tenant_id == tenant_id) if tenant_id is not None else statement


async def _audit(db: AsyncSession, draft: HrCreationDraft, *, event_type: str, action: str, details: dict[str, Any]):
    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type=event_type,
        severity="warning" if "reconcile" in action or "block" in action else "info",
        actor_type="system",
        actor_id=None,
        tenant_id=draft.tenant_id,
        action=action,
        resource_type="hr_creation_draft",
        resource_id=draft.id,
        details=details,
    )


async def _expire_preview(db: AsyncSession, draft: HrCreationDraft, *, now: datetime) -> bool:
    if draft.expires_at is None:
        created_at = draft.created_at or now
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        draft.expires_at = created_at + HR_CREATION_PREVIEW_TTL
    if draft.expires_at > now:
        return False
    draft.status = "expired"
    draft.provisioning_json = {
        **dict(draft.provisioning_json or {}),
        "expired_at": now.isoformat(),
        "expiry_reason": "confirmation_ttl_elapsed",
    }
    await _audit(
        db,
        draft,
        event_type="hr.creation_draft_expired",
        action="expire_hr_creation_draft",
        details={"expires_at": draft.expires_at.isoformat()},
    )
    return True


async def _block_missing_authority(
    db: AsyncSession,
    draft: HrCreationDraft,
    task: RuntimeTask | None,
    *,
    now: datetime,
) -> None:
    if task is not None:
        uncertain = task.status in {"running", "completed", "needs_reconciliation"}
        if task.status == "running" or task.claimed_by is not None:
            task.claim_version = int(task.claim_version or 0) + 1
        task.status = "needs_reconciliation" if uncertain else "killed"
        task.completed_at = now
        task.claimed_by = None
        task.claim_expires_at = None
        task.result_summary = "HR provisioning lacks authenticated confirmation evidence."
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "phase": "terminal",
            "automatic_retry_allowed": False,
            "needs_reconciliation": uncertain,
            "reconciliation_reason": "missing_confirmation_evidence",
        }
    _clear_stale_claim(draft)
    draft.status = "failed"
    draft.failure_code = "missing_confirmation_evidence"
    draft.failure_message = "Provisioning cannot resume without authenticated blueprint confirmation."
    await _audit(
        db,
        draft,
        event_type="hr.creation_authority_blocked",
        action="block_hr_provisioning_without_confirmation",
        details={"provisioning_task_id": str(task.id) if task is not None else None},
    )


async def _recover_missing_job(
    db: AsyncSession,
    draft: HrCreationDraft,
    *,
    now: datetime,
) -> RuntimeTask:
    task = build_hr_provisioning_runtime_task(draft)
    if draft.status == "failed":
        task.status = "failed"
        task.completed_at = now
        task.result_summary = draft.failure_message or "Legacy failed HR draft requires an explicit retry."
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "phase": "terminal",
            "automatic_retry_allowed": True,
            "recovered_missing_job": True,
            "outcome": {"status": "failed", "draft_status": "failed"},
        }
    else:
        task.metadata_json = {**dict(task.metadata_json or {}), "recovered_missing_job": True}
    db.add(task)
    await db.flush()
    draft.provisioning_task_id = task.id
    if draft.status in {"creating", "provisioning"}:
        _clear_stale_claim(draft)
    draft.provisioning_json = {
        **dict(draft.provisioning_json or {}),
        "runtime_task_id": str(task.id),
        "runtime_status": task.status,
        "runtime_phase": "reconciled_missing_job",
    }
    await _audit(
        db,
        draft,
        event_type="hr.creation_job_recovered",
        action="recover_hr_provisioning_job",
        details={"provisioning_task_id": str(task.id), "task_status": task.status},
    )
    return task


async def _converge_terminal_task(
    db: AsyncSession,
    draft: HrCreationDraft,
    task: RuntimeTask,
) -> None:
    previous_status = task.status
    if previous_status == "completed":
        task.status = "needs_reconciliation"
        task.result_summary = "HR RuntimeTask completed while its canonical draft remained incomplete."
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "needs_reconciliation": True,
            "reconciliation_reason": "hr_runtime_terminal_draft_divergence",
            "automatic_retry_allowed": False,
        }
        failure_code = "runtime_task_draft_divergence"
    elif previous_status == "needs_reconciliation":
        failure_code = "runtime_task_needs_reconciliation"
    else:
        failure_code = "runtime_task_failed"
    _clear_stale_claim(draft)
    draft.status = "failed"
    draft.failure_code = failure_code
    draft.failure_message = task.result_summary or f"HR provisioning task ended as {previous_status}."
    draft.provisioning_json = {
        **dict(draft.provisioning_json or {}),
        "runtime_status": task.status,
        "runtime_phase": "terminal_reconciled",
    }
    if draft.created_agent_id is not None:
        employee = await db.get(Agent, draft.created_agent_id, with_for_update=True)
        if employee is not None and employee.deleted_at is None:
            employee.status = "error"
    await _audit(
        db,
        draft,
        event_type="hr.creation_terminal_reconciled",
        action="reconcile_hr_terminal_state",
        details={"task_status": previous_status, "failure_code": failure_code},
    )


def _release_claim_for_replay(draft: HrCreationDraft, task: RuntimeTask) -> None:
    _clear_stale_claim(draft)
    draft.provisioning_json = {
        **dict(draft.provisioning_json or {}),
        "runtime_status": task.status,
        "runtime_phase": "stale_claim_released",
    }


async def reconcile_hr_creation_drafts_once(
    *,
    limit: int = 100,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Converge expired previews and orphaned HR state without model replay."""

    import app.database as database
    from app.database import enter_rls_bypass

    now = _utcnow()
    summary = dict.fromkeys(
        ("checked", "expired", "jobs_created", "claims_released", "failed_converged", "authority_blocked"),
        0,
    )
    async with (
        database.async_session() as db,
        enter_rls_bypass(db, reason="HR draft expiry and orphaned provisioning reconciliation"),
    ):
        statement = _candidate_statement(
            select(HrCreationDraft.id, HrCreationDraft.provisioning_task_id),
            now=now,
            limit=limit,
            tenant_id=tenant_id,
        )
        candidates = list((await db.execute(statement)).all())
        summary["checked"] = len(candidates)
        for draft_id, linked_task_id in candidates:
            # All HR mutations share RuntimeTask -> draft lock order. Candidate
            # discovery is intentionally unlocked; the fresh locked row below
            # is the authority and stale candidates are skipped.
            task = (
                await db.get(RuntimeTask, linked_task_id, with_for_update=True) if linked_task_id is not None else None
            )
            draft = await db.get(HrCreationDraft, draft_id, populate_existing=True, with_for_update=True)
            if draft is None or draft.provisioning_task_id != linked_task_id:
                continue
            if draft.status == "awaiting_confirmation":
                summary["expired"] += int(await _expire_preview(db, draft, now=now))
            elif draft.status not in _ACTIVE_DRAFT_STATUSES:
                continue
            elif draft.confirmed_by_user_id is None or draft.confirmed_at is None:
                await _block_missing_authority(db, draft, task, now=now)
                summary["authority_blocked"] += 1
            elif task is None:
                await _recover_missing_job(db, draft, now=now)
                summary["jobs_created"] += 1
            elif draft.status in {"confirmed", "creating", "provisioning"} and task.status in _TERMINAL_TASK_STATUSES:
                await _converge_terminal_task(db, draft, task)
                summary["failed_converged"] += 1
            elif _has_stale_domain_claim(draft, task, now=now):
                _release_claim_for_replay(draft, task)
                summary["claims_released"] += 1
        await db.commit()
    return summary
