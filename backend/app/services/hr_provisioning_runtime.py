"""Durable RuntimeTask ownership for confirmed HR provisioning.

The authenticated confirmation transaction creates exactly one
``hr_provisioning`` task.  This worker is the only production consumer; the
chat model never has to restate the blueprint or call the creation tool after
confirmation.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.services.runtime_task_fence import assert_runtime_task_fence, renew_current_runtime_task_lease


HR_PROVISIONING_JOB_SCHEMA = "hr_provisioning_job.v1"
_TERMINAL_TASK_STATUSES = {"completed", "failed", "killed", "skipped", "needs_reconciliation"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_hr_provisioning_runtime_task(draft: HrCreationDraft) -> RuntimeTask:
    """Build the one replay-safe job for an immutable confirmed blueprint."""

    if draft.id is None or draft.tenant_id is None:
        raise ValueError("HR provisioning requires a persisted, tenant-bound draft")
    task_id = uuid.uuid4()
    session_id = str(draft.session_id)
    metadata = {
        "schema": HR_PROVISIONING_JOB_SCHEMA,
        "draft_id": str(draft.id),
        "blueprint_version": int(draft.blueprint_version),
        "blueprint_hash": str(draft.blueprint_hash),
        "phase": "queued",
        "side_effect_risk": "not_started",
        "automatic_retry_allowed": True,
        "queued_at": _utcnow().isoformat(),
    }
    config = {
        "task_type": "hr_provisioning",
        "draft_id": str(draft.id),
        "blueprint_version": int(draft.blueprint_version),
        "blueprint_hash": str(draft.blueprint_hash),
        "hr_agent_id": str(draft.hr_agent_id),
        "session_id": session_id,
    }
    policy = {
        "tenant_id": str(draft.tenant_id),
        "requested_by_user_id": str(draft.requested_by_user_id),
        "confirmed_by_user_id": str(draft.confirmed_by_user_id) if draft.confirmed_by_user_id else None,
        "confirmed_at": draft.confirmed_at.isoformat() if draft.confirmed_at else None,
    }
    return RuntimeTask(
        id=task_id,
        task_type="hr_provisioning",
        parent_agent_id=draft.hr_agent_id,
        tenant_id=draft.tenant_id,
        status="pending",
        scheduled_at=_utcnow(),
        priority=24,
        prompt="Provision the authenticated canonical HR blueprint.",
        trace_id=f"hr-provisioning:{draft.id}",
        parent_session_id=session_id,
        root_user_id=draft.requested_by_user_id,
        root_session_id=session_id,
        delegation_chain_json=[f"agent:{draft.hr_agent_id}"],
        depth=1,
        root_idempotency_key=f"hr-provisioning:{draft.id}-v{draft.blueprint_version}",
        config_snapshot_hash=_snapshot_hash(config),
        policy_snapshot_hash=_snapshot_hash(policy),
        metadata_json=metadata,
    )


def _clear_runtime_claim(task: RuntimeTask) -> None:
    task.claimed_by = None
    task.claim_expires_at = None


def _mark_task_completed(task: RuntimeTask, draft: HrCreationDraft) -> str:
    now = _utcnow()
    metadata = dict(task.metadata_json or {})
    summary = f"Digital employee provisioned: {draft.created_agent_id or 'completed'}"
    task.status = "completed"
    task.completed_at = task.completed_at or now
    task.result_summary = summary
    _clear_runtime_claim(task)
    metadata.update(
        {
            "phase": "terminal",
            "terminal_at": now.isoformat(),
            "side_effect_risk": "confirmed",
            "automatic_retry_allowed": False,
            "outcome": {
                "status": "completed",
                "created_agent_id": str(draft.created_agent_id) if draft.created_agent_id else None,
            },
        }
    )
    task.metadata_json = metadata
    return "completed"


def _mark_task_failed(task: RuntimeTask, draft: HrCreationDraft, *, reason: str) -> str:
    now = _utcnow()
    metadata = dict(task.metadata_json or {})
    summary = draft.failure_message or reason or "HR provisioning did not complete."
    task.status = "failed"
    task.completed_at = now
    task.result_summary = summary
    _clear_runtime_claim(task)
    metadata.update(
        {
            "phase": "terminal",
            "terminal_at": now.isoformat(),
            "side_effect_risk": "journaled",
            "automatic_retry_allowed": True,
            "outcome": {
                "status": "failed",
                "draft_status": draft.status,
                "failure_code": draft.failure_code,
                "summary": summary,
            },
        }
    )
    task.metadata_json = metadata
    return "failed"


def _schedule_for_draft_claim(task: RuntimeTask, draft: HrCreationDraft, *, reason: str) -> str:
    metadata = dict(task.metadata_json or {})
    task.status = "resumable"
    task.scheduled_at = draft.claim_expires_at or (_utcnow() + timedelta(seconds=30))
    task.completed_at = None
    _clear_runtime_claim(task)
    metadata.update(
        {
            "phase": "waiting_draft_claim_expiry",
            "wait_reason": reason,
            "automatic_retry_allowed": True,
            "side_effect_risk": "journaled_or_in_progress",
        }
    )
    task.metadata_json = metadata
    return "waiting_claim_expiry"


async def _load_tenant_id(task_id: uuid.UUID) -> uuid.UUID | None:
    import app.database as database
    from app.services.tenant_resolver import resolve_tenant_for_runtime_task

    return await resolve_tenant_for_runtime_task(task_id, session_factory=database.async_session)


async def _prepare_hr_provisioning(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> tuple[str, RuntimeTask | None, HrCreationDraft | None]:
    import app.database as database

    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="hr_provisioning_prepare",
    ) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        if task is None:
            return "missing", None, None
        assert_runtime_task_fence(task)
        draft = (
            await db.execute(
                select(HrCreationDraft)
                .where(
                    HrCreationDraft.provisioning_task_id == task_id,
                    HrCreationDraft.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if draft is None:
            now = _utcnow()
            metadata = dict(task.metadata_json or {})
            task.status = "needs_reconciliation"
            task.completed_at = now
            task.result_summary = "HR provisioning task has no tenant-bound draft link."
            _clear_runtime_claim(task)
            metadata.update(
                {
                    "phase": "terminal",
                    "terminal_at": now.isoformat(),
                    "needs_reconciliation": True,
                    "reconciliation_reason": "hr_draft_link_missing",
                    "automatic_retry_allowed": False,
                }
            )
            task.metadata_json = metadata
            return "needs_reconciliation", task, None
        if draft.status == "completed":
            return _mark_task_completed(task, draft), task, draft
        if draft.status in {"rejected", "superseded", "expired"}:
            task.status = "skipped" if draft.status != "rejected" else "killed"
            task.completed_at = _utcnow()
            task.result_summary = f"HR draft is {draft.status}."
            _clear_runtime_claim(task)
            task.metadata_json = {
                **dict(task.metadata_json or {}),
                "phase": "terminal",
                "automatic_retry_allowed": False,
                "outcome": {"status": task.status, "draft_status": draft.status},
            }
            return task.status, task, draft
        metadata = dict(task.metadata_json or {})
        live_draft_claim = bool(draft.claim_token and draft.claim_expires_at and draft.claim_expires_at > _utcnow())
        if draft.status in {"creating", "provisioning"} and live_draft_claim:
            return (
                _schedule_for_draft_claim(
                    task,
                    draft,
                    reason="A previous fenced worker still owns the domain provisioning lease.",
                ),
                task,
                draft,
            )
        if task.status != "running":
            return str(task.status or "not_claimed"), task, draft
        metadata.update(
            {
                "phase": "executing",
                "execution_started_at": _utcnow().isoformat(),
                "side_effect_risk": "journaled_steps_may_start",
                "automatic_retry_allowed": False,
            }
        )
        task.metadata_json = metadata
        return "execute", task, draft


async def _run_domain_provisioning(*, task: RuntimeTask, draft: HrCreationDraft) -> str:
    """Invoke the existing single HR lifecycle owner with trusted canonical IDs."""

    from app.services.hr_provisioning_runner import run_hr_provisioning
    from app.tools.handlers import hr as hr_handler
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    from app.tools.workspace import ensure_workspace

    workspace = await ensure_workspace(draft.hr_agent_id, tenant_id=str(draft.tenant_id))
    request = ToolExecutionRequest(
        tool_name="create_digital_employee",
        arguments={"blueprint_id": str(draft.id)},
        context=ToolExecutionContext(
            agent_id=draft.hr_agent_id,
            user_id=draft.requested_by_user_id,
            tenant_id=str(draft.tenant_id),
            workspace=workspace,
            session_id=str(draft.session_id),
            runtime_task_id=str(task.id),
            emit_runtime_hooks=False,
        ),
    )
    return await run_hr_provisioning(request=request, support=sys.modules[hr_handler.__name__])


async def _converge_hr_provisioning(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    runner_result: str | None,
    runner_error: str | None,
) -> str:
    import app.database as database

    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="hr_provisioning_converge",
    ) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        if task is None:
            return "missing"
        assert_runtime_task_fence(task)
        draft = (
            await db.execute(
                select(HrCreationDraft)
                .where(
                    HrCreationDraft.provisioning_task_id == task_id,
                    HrCreationDraft.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if draft is None:
            task.status = "needs_reconciliation"
            task.completed_at = _utcnow()
            task.result_summary = "HR draft disappeared after provisioning execution."
            task.metadata_json = {
                **dict(task.metadata_json or {}),
                "phase": "terminal",
                "needs_reconciliation": True,
                "automatic_retry_allowed": False,
            }
            return "needs_reconciliation"
        if draft.status == "completed":
            return _mark_task_completed(task, draft)
        if draft.status == "failed":
            return _mark_task_failed(task, draft, reason=runner_error or runner_result or "Provisioning failed.")
        live_claim = bool(draft.claim_token and draft.claim_expires_at and draft.claim_expires_at > _utcnow())
        if draft.status in {"creating", "provisioning"} and live_claim:
            return _schedule_for_draft_claim(
                task,
                draft,
                reason=runner_error or "The domain provisioning lease is still active.",
            )
        if draft.status == "provisioning":
            return _mark_task_failed(
                task,
                draft,
                reason=runner_error or runner_result or "Required provisioning steps remain incomplete.",
            )
        now = _utcnow()
        task.status = "needs_reconciliation"
        task.completed_at = now
        task.result_summary = runner_error or runner_result or f"Unexpected HR draft state: {draft.status}"
        _clear_runtime_claim(task)
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "phase": "terminal",
            "terminal_at": now.isoformat(),
            "needs_reconciliation": True,
            "reconciliation_reason": f"unexpected_hr_draft_state:{draft.status}",
            "automatic_retry_allowed": False,
        }
        return "needs_reconciliation"


async def execute_claimed_hr_provisioning(task_id: uuid.UUID) -> str:
    """Consume a claimed HR job or idempotently converge its persisted state."""

    tenant_id = await _load_tenant_id(task_id)
    if tenant_id is None:
        logger.error("[HRProvisioning] RuntimeTask {} has no tenant authority", task_id)
        return "missing_tenant"
    disposition, task, draft = await _prepare_hr_provisioning(task_id, tenant_id)
    if disposition != "execute":
        return disposition
    if task is None or draft is None:
        return "needs_reconciliation"
    await renew_current_runtime_task_lease(lease_seconds=600.0)
    result: str | None = None
    error: str | None = None
    try:
        result = await _run_domain_provisioning(task=task, draft=draft)
    except Exception as exc:  # The draft journal decides whether replay is safe.
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("[HRProvisioning] task {} failed before convergence", task_id)
    return await _converge_hr_provisioning(
        task_id,
        tenant_id,
        runner_result=result,
        runner_error=error,
    )
