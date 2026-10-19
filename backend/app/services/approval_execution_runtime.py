"""Durable worker ownership for approved tool execution.

The approval decision and the ``approval_execution`` RuntimeTask are committed
atomically.  This module is the only worker-side consumer.  It deliberately
does not replay a ticket once ``consumed_at`` proves that an external side
effect may have started; that state is quarantined for operator review.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ApprovalRequest
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.services.approval_ticket import ApprovalTicketError
from app.services.runtime_task_fence import assert_runtime_task_fence, renew_current_runtime_task_lease


APPROVAL_EXECUTION_JOB_SCHEMA = "approval_execution_job.v1"
_SAFE_PRE_EXECUTION_STATES = {"queued", "approved"}
_TERMINAL_EXECUTION_STATES = {"succeeded", "failed", "needs_reapproval", "needs_reconciliation"}
_MAX_SAFE_DISPATCH_ATTEMPTS = 3


def approval_notification_summary(status: str, result: str, receipt=None) -> str:
    """Only a confirmed execution carries returned business bytes into a continuation."""
    summaries = {
        "needs_reconciliation": "The approved action may have taken effect. Verify its outcome before retrying.",
        "needs_reapproval": "The action requires a new approval before it can run.",
        "rejected": "The action was rejected and was not executed.",
        "failed": "The approved action did not finish successfully. Review its receipt before retrying.",
    }
    summary = summaries.get(status, result)
    # Only executor-authored machine facts cross the failed-result boundary;
    # a provider's unverified body remains in its original scoped receipt.
    receipt = receipt or {}
    evidence = receipt.get("runtime_evidence") or {}
    facts = evidence.get("feishu_failure")
    frame = evidence.get("tool_execution_frame") or {}
    error = receipt.get("error") or {}
    if (status == "failed" and receipt.get("status") == status and isinstance(facts, dict)
        and set(facts) == {"schema", "tool_name", "error_class", "http_status", "provider_code", "retryable", "next_action"}
        and facts["schema"] == "hive.feishu_failure.v1"
        and facts["tool_name"] == receipt.get("tool_name") == frame.get("tool_name")
        and frame.get("status") == "failed"
        and frame.get("output_hash") == hashlib.sha256(json.dumps(result, ensure_ascii=False).encode()).hexdigest()
        and facts["error_class"] in {"not_configured", "invalid_arguments", "invalid_approval_form", "provider_bad_request", "auth_or_permission", "quota_or_billing",
            "not_found", "timeout", "rate_limited", "provider_unavailable", "provider_error"}
        and facts["next_action"] in {"configure_connection", "review_request", "reconnect", "review_permissions", "retry_later", "review_provider_error"}
        and type(facts["retryable"]) is bool
        and all(facts[k] is None or type(facts[k]) is int for k in ("http_status", "provider_code"))
        and all(facts[k] == error.get(k) for k in ("tool_name", "error_class", "http_status", "provider_code", "retryable", "next_action"))):
        summary += "\n" + json.dumps(facts, ensure_ascii=False, sort_keys=True)
    return summary


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_approval_execution_task(
    approval: ApprovalRequest,
    *,
    approved_by_user_id: uuid.UUID,
) -> RuntimeTask:
    """Build the one durable execution job linked to an approved ticket."""

    if approval.tenant_id is None:
        raise ApprovalTicketError("approved tool execution requires tenant authority")
    task_id = uuid.uuid4()
    details = dict(approval.details or {})
    session_id = str(details.get("session_id") or details.get("conversation_id") or "").strip() or None
    root_user_id = approval.requested_by or approved_by_user_id
    metadata = {
        "schema": APPROVAL_EXECUTION_JOB_SCHEMA,
        "approval_id": str(approval.id),
        "approved_by_user_id": str(approved_by_user_id),
        "tool_name": str(approval.tool_name or approval.action_type or "approved_action"),
        "origin_session_key": session_id,
        "phase": "queued",
        "side_effect_risk": "not_started",
        "reconciliation_retry_allowed": True,
        "queued_at": _utcnow().isoformat(),
    }
    config_snapshot = {
        "task_type": "approval_execution",
        "approval_id": str(approval.id),
        "agent_id": str(approval.agent_id),
        "tenant_id": str(approval.tenant_id),
        "session_id": session_id,
    }
    policy_hash = str(approval.policy_snapshot_hash or "").strip() or _snapshot_hash(
        {"approval_id": str(approval.id), "policy": "missing_requires_runtime_recheck"}
    )
    return RuntimeTask(
        id=task_id,
        task_type="approval_execution",
        parent_agent_id=approval.agent_id,
        tenant_id=approval.tenant_id,
        status="pending",
        scheduled_at=_utcnow(),
        priority=25,
        prompt=f"Execute approved action: {approval.action_type}",
        trace_id=f"approval:{approval.id}",
        parent_session_id=session_id,
        root_user_id=root_user_id,
        root_session_id=session_id,
        delegation_chain_json=[f"agent:{approval.agent_id}"],
        depth=1,
        root_idempotency_key=f"approval-execution:{approval.id}",
        config_snapshot_hash=_snapshot_hash(config_snapshot),
        policy_snapshot_hash=policy_hash,
        metadata_json=metadata,
    )


def _set_task_terminal(
    task: RuntimeTask,
    *,
    approval_status: str,
    result: str | None,
) -> str:
    now = _utcnow()
    metadata = dict(task.metadata_json or {})
    if approval_status == "succeeded":
        task.status = "completed"
        side_effect_risk = "confirmed"
    elif approval_status == "needs_reconciliation":
        task.status = "needs_reconciliation"
        side_effect_risk = "unknown"
    else:
        task.status = "failed"
        side_effect_risk = "not_started" if approval_status == "needs_reapproval" else "confirmed_failed"
    task.completed_at = task.completed_at or now
    task.result_summary = result or f"Approval execution {approval_status}"
    metadata.update(
        {
            "phase": "terminal",
            "terminal_at": now.isoformat(),
            "side_effect_risk": side_effect_risk,
            "reconciliation_retry_allowed": False,
            "outcome": {
                "status": approval_status,
                "summary": task.result_summary,
            },
        }
    )
    if approval_status == "needs_reconciliation":
        metadata.update(
            {
                "needs_reconciliation": True,
                "reconciliation_status": "open",
                "reconciliation_reason": "approval_execution_side_effect_unknown",
            }
        )
    task.metadata_json = metadata
    return approval_status


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


async def _resolve_origin_session(
    db: AsyncSession,
    *,
    approval: ApprovalRequest,
    task: RuntimeTask | None,
) -> tuple[ChatSession | None, str | None]:
    details = dict(approval.details or {})
    session_key = str(
        details.get("session_id") or details.get("conversation_id") or (task.parent_session_id if task else None) or ""
    ).strip()
    if not session_key:
        return None, None
    session_id = _uuid_or_none(session_key)
    query = select(ChatSession).where(
        ChatSession.tenant_id == approval.tenant_id,
        ChatSession.agent_id == approval.agent_id,
    )
    if session_id is not None:
        query = query.where(ChatSession.id == session_id)
    else:
        query = query.where(ChatSession.external_conv_id == session_key)
    return (await db.execute(query)).scalar_one_or_none(), session_key


async def _enqueue_origin_continuation(
    db: AsyncSession,
    *,
    approval: ApprovalRequest,
    task: RuntimeTask | None,
    approval_status: str,
    execution_result: str | None,
) -> uuid.UUID | None:
    """Atomically bridge an approval decision or execution back to its source session."""
    session, session_key = await _resolve_origin_session(db, approval=approval, task=task)
    receipt = dict(approval.execution_receipt or {})
    parent_user_id = session.user_id if session else None
    root_runtime_task_id = None
    if session is not None and session.audience_kind == "group":
        from app.services.session_approval import group_approval_requester

        actor, invocation = await group_approval_requester(db, session=session, approval=approval)
        parent_user_id = actor.id
        original = await db.get(RuntimeTask, invocation.run_id)
        root_runtime_task_id = original.root_runtime_task_id or original.id
    if session_key is None:
        receipt["continuation_status"] = "not_requested"
        approval.execution_receipt = receipt
        return None
    if session is None or parent_user_id is None or approval.tenant_id is None:
        receipt.update(
            {
                "continuation_status": "needs_reconciliation",
                "continuation_reason": "origin_session_authority_unresolved",
                "origin_session_key": session_key,
            }
        )
        approval.execution_receipt = receipt
        if task is not None:
            metadata = dict(task.metadata_json or {})
            metadata["continuation_status"] = "needs_reconciliation"
            metadata["continuation_reason"] = "origin_session_authority_unresolved"
            task.metadata_json = metadata
        return None

    from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
    from app.services.feishu_read_receipts import FEISHU_RESULT_TOOLS, load_approved_feishu_result

    tool_name = str(approval.tool_name or approval.action_type or "approved_action")
    verified_failure = (tool_name in FEISHU_RESULT_TOOLS and approval_status == "failed"
        and await load_approved_feishu_result(db, approval=approval) is not None)
    status_only = approval_status == "needs_reconciliation" or (
        tool_name in FEISHU_RESULT_TOOLS and approval_status != "succeeded" and not verified_failure)
    result_text = str(execution_result or (task.result_summary if task else None) or "")
    if status_only:
        result_text = approval_notification_summary(approval_status, result_text, approval.execution_receipt)
    model_context = (
        "[Approval tool result]\n"
        f"Approval: {approval.id}\n"
        f"Status: {approval_status}\n"
        f"Tool: {tool_name}\n"
        f"Result: {result_text}\n"
        "Treat this as the result of the previously approval-gated tool call and continue the original task."
    )
    terminal_status = {
        "succeeded": "completed",
        "failed": "failed",
        "needs_reapproval": "blocked",
        "needs_reconciliation": "needs_reconciliation",
        "rejected": "blocked",
    }.get(approval_status, approval_status)
    outbox_id = await enqueue_completion_notification(
        db,
        CompletionNotification(
            tenant_id=approval.tenant_id,
            source_kind="approval",
            source_run_id=str(task.id if task else approval.id),
            parent_session_id=session.id,
            parent_agent_id=approval.agent_id,
            parent_user_id=parent_user_id,
            root_runtime_task_id=root_runtime_task_id,
            terminal_status=terminal_status,
            task_type="approval_execution" if task else "approval_decision",
            summary=result_text or f"Approval execution {approval_status}.",
            delivery_mode="session_projection" if approval_status == "needs_reconciliation" else "parent_continuation",
            metadata={
                "approval_id": str(approval.id),
                "approval_status": approval_status,
                "tool_name": tool_name,
                "model_context": model_context,
                "origin_session_id": str(session.id),
                "result_semantics": "approval_status" if status_only else "execution_result",
            },
            payload_rank=100,
        ),
    )
    receipt.update(
        {
            "continuation_status": "queued",
            "continuation_outbox_id": str(outbox_id),
            "origin_session_id": str(session.id),
        }
    )
    approval.execution_receipt = receipt
    if task is not None:
        metadata = dict(task.metadata_json or {})
        metadata.update(
            {
                "completion_outbox_id": str(outbox_id),
                "continuation_status": "queued",
                "origin_session_id": str(session.id),
                "approval_id": str(approval.id),
                "tool_name": tool_name,
            }
        )
        task.metadata_json = metadata
    return outbox_id


def _quarantine_unknown_execution(task: RuntimeTask, approval: ApprovalRequest) -> str:
    now = _utcnow()
    receipt = dict(approval.execution_receipt or {})
    receipt.update(
        {
            "status": "needs_reconciliation",
            "side_effect_state": "unknown",
            "automatic_replay": False,
            "execution_task_id": str(task.id),
            "reconciled_at": now.isoformat(),
        }
    )
    approval.execution_status = "needs_reconciliation"
    approval.execution_receipt = receipt
    return _set_task_terminal(
        task,
        approval_status="needs_reconciliation",
        result="Approval execution was consumed before worker recovery; external side effects are unknown.",
    )


async def _load_tenant_id(task_id: uuid.UUID) -> uuid.UUID | None:
    import app.database as database
    from app.services.tenant_resolver import resolve_tenant_for_runtime_task

    return await resolve_tenant_for_runtime_task(task_id, session_factory=database.async_session)


async def _prepare_execution(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> tuple[str, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
    import app.database as database

    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="approval_execution_prepare",
    ) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        if task is None:
            return "missing", None, None, None
        assert_runtime_task_fence(task)
        approval = (
            await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.execution_task_id == task_id,
                    ApprovalRequest.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if approval is None:
            metadata = dict(task.metadata_json or {})
            metadata.update(
                {
                    "needs_reconciliation": True,
                    "reconciliation_status": "open",
                    "reconciliation_reason": "approval_execution_link_missing",
                    "side_effect_risk": "unknown",
                    "reconciliation_retry_allowed": False,
                }
            )
            task.metadata_json = metadata
            task.status = "needs_reconciliation"
            task.completed_at = _utcnow()
            task.result_summary = "Approval execution task has no tenant-bound ApprovalRequest link."
            return "needs_reconciliation", None, None, None

        execution_status = str(approval.execution_status or "pending")
        if execution_status in _TERMINAL_EXECUTION_STATES:
            if execution_status == "needs_reconciliation" and not (approval.execution_receipt or {}).get("continuation_outbox_id"):
                await _enqueue_origin_continuation(db, approval=approval, task=task,
                    approval_status=execution_status, execution_result=approval.execution_result)
            return (
                _set_task_terminal(
                    task,
                    approval_status=execution_status,
                    result=approval.execution_result,
                ),
                approval.id,
                approval.resolved_by,
                approval.agent_id,
            )
        if approval.consumed_at is not None or execution_status == "executing":
            disposition = _quarantine_unknown_execution(task, approval)
            await _enqueue_origin_continuation(db, approval=approval, task=task,
                approval_status=disposition, execution_result=approval.execution_result)
            return disposition, approval.id, approval.resolved_by, approval.agent_id
        if execution_status not in _SAFE_PRE_EXECUTION_STATES:
            approval.execution_status = "needs_reapproval"
            approval.execution_result = f"Approval execution cannot start from state {execution_status}."
            approval.execution_receipt = {
                **dict(approval.execution_receipt or {}),
                "status": "needs_reapproval",
                "side_effect_state": "not_started",
                "automatic_replay": False,
            }
            return (
                _set_task_terminal(
                    task,
                    approval_status="needs_reapproval",
                    result=approval.execution_result,
                ),
                approval.id,
                approval.resolved_by,
                approval.agent_id,
            )
        if task.status != "running":
            return "not_claimed", approval.id, approval.resolved_by, approval.agent_id
        metadata = dict(task.metadata_json or {})
        metadata.update(
            {
                "phase": "executing",
                "execution_started_at": _utcnow().isoformat(),
                "side_effect_risk": "may_start",
                "reconciliation_retry_allowed": False,
            }
        )
        task.metadata_json = metadata
        return "execute", approval.id, approval.resolved_by, approval.agent_id


async def _finalize_from_ticket(task_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[str, str | None]:
    import app.database as database

    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="approval_execution_finalize",
    ) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        if task is None:
            return "missing", None
        assert_runtime_task_fence(task)
        approval = (
            await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.execution_task_id == task_id,
                    ApprovalRequest.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if approval is None:
            return "missing", None
        status = str(approval.execution_status or "pending")
        if status == "executing" or approval.consumed_at is not None and status not in _TERMINAL_EXECUTION_STATES:
            disposition = _quarantine_unknown_execution(task, approval)
            await _enqueue_origin_continuation(db, approval=approval, task=task,
                approval_status=disposition, execution_result=approval.execution_result)
            return disposition, approval.execution_result
        if status not in _TERMINAL_EXECUTION_STATES:
            status = "failed"
            approval.execution_status = status
            approval.execution_result = "Approved tool returned without a terminal execution receipt."
            approval.execution_receipt = {
                **dict(approval.execution_receipt or {}),
                "status": status,
                "side_effect_state": "unknown",
                "automatic_replay": False,
            }
        terminal_status = _set_task_terminal(task, approval_status=status, result=approval.execution_result)
        await _enqueue_origin_continuation(
            db,
            approval=approval,
            task=task,
            approval_status=status,
            execution_result=approval.execution_result,
        )
        return terminal_status, approval.execution_result


async def _mark_preflight_reapproval(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    error: str,
) -> str:
    import app.database as database

    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="approval_execution_reapproval",
    ) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        if task is None:
            return "missing"
        assert_runtime_task_fence(task)
        approval = (
            await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.execution_task_id == task_id, ApprovalRequest.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if approval is None:
            return "missing"
        if approval.consumed_at is not None or approval.execution_status == "executing":
            return _quarantine_unknown_execution(task, approval)
        approval.execution_status = "needs_reapproval"
        approval.execution_result = error
        approval.execution_receipt = {
            **dict(approval.execution_receipt or {}),
            "status": "needs_reapproval",
            "side_effect_state": "not_started",
            "automatic_replay": False,
            "error": error,
        }
        return _set_task_terminal(task, approval_status="needs_reapproval", result=error)


async def _handle_operational_failure(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    error: str,
) -> str:
    import app.database as database

    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="approval_execution_operational_failure",
    ) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        if task is None:
            return "missing"
        assert_runtime_task_fence(task)
        approval = (
            await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.execution_task_id == task_id, ApprovalRequest.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if approval is None:
            return "missing"
        status = str(approval.execution_status or "pending")
        if status in _TERMINAL_EXECUTION_STATES:
            return _set_task_terminal(task, approval_status=status, result=approval.execution_result)
        if approval.consumed_at is not None or status == "executing":
            return _quarantine_unknown_execution(task, approval)
        attempts = int(task.attempt_count or 0)
        metadata = dict(task.metadata_json or {})
        if attempts < _MAX_SAFE_DISPATCH_ATTEMPTS:
            task.status = "resumable"
            task.scheduled_at = _utcnow() + timedelta(seconds=max(5, attempts * 5))
            task.claimed_by = None
            task.claim_expires_at = None
            metadata.update(
                {
                    "phase": "retry_wait",
                    "retry_reason": error,
                    "retry_attempt": attempts,
                    "side_effect_risk": "not_started",
                    "reconciliation_retry_allowed": True,
                }
            )
            task.metadata_json = metadata
            return "retry_scheduled"
        approval.execution_status = "failed"
        approval.execution_result = error
        approval.execution_receipt = {
            **dict(approval.execution_receipt or {}),
            "status": "failed",
            "side_effect_state": "not_started",
            "automatic_replay": False,
            "attempt_count": attempts,
            "error": error,
        }
        return _set_task_terminal(task, approval_status="failed", result=error)


async def execute_claimed_approval_execution(task_id: uuid.UUID) -> str:
    """Consume one claimed approval job, or safely converge its persisted state."""

    tenant_id = await _load_tenant_id(task_id)
    if tenant_id is None:
        logger.error("[ApprovalExecution] RuntimeTask {} has no tenant authority", task_id)
        return "missing_tenant"
    disposition, approval_id, approved_by_user_id, expected_agent_id = await _prepare_execution(
        task_id,
        tenant_id,
    )
    if disposition != "execute":
        return disposition
    if approval_id is None or approved_by_user_id is None or expected_agent_id is None:
        return await _mark_preflight_reapproval(
            task_id,
            tenant_id,
            error="Approval execution principal binding is incomplete.",
        )

    await renew_current_runtime_task_lease(lease_seconds=180.0)
    try:
        from app.services.agent_tools import execute_approved_tool

        await execute_approved_tool(
            approval_id=approval_id,
            expected_agent_id=expected_agent_id,
            approved_by_user_id=approved_by_user_id,
        )
    except ApprovalTicketError as exc:
        return await _mark_preflight_reapproval(
            task_id,
            tenant_id,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # ToolRuntime persists terminal ticket state before raising when execution started.
        return await _handle_operational_failure(
            task_id,
            tenant_id,
            error=f"{type(exc).__name__}: {exc}",
        )

    status, _persisted_result = await _finalize_from_ticket(task_id, tenant_id)
    return status
