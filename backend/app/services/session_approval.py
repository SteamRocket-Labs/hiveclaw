"""Read an enterprise tool ticket through its immutable originating-session binding."""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select

from app.models.audit import ApprovalRequest
from app.services.approval_ticket import ApprovalTicketError, restore_approval_execution_envelope
from app.services.enterprise_approval_visibility import is_visible_enterprise_approval

logger = logging.getLogger(__name__)


async def load_session_approval(db, *, session, approval_id, for_update=False):
    query = select(ApprovalRequest).where(
        ApprovalRequest.id == approval_id,
        ApprovalRequest.tenant_id == session.tenant_id,
        ApprovalRequest.agent_id == session.agent_id,
    )
    approval = await db.scalar(query.with_for_update() if for_update else query)
    if approval is None or not approval.tool_name or not is_visible_enterprise_approval(approval):
        return None
    try:
        envelope = restore_approval_execution_envelope(
            approval.execution_envelope, expected_hash=approval.execution_envelope_hash,
        )
    except (ApprovalTicketError, TypeError, ValueError):
        return None
    if (
        envelope.tenant_id != session.tenant_id or envelope.agent_id != session.agent_id
        or envelope.requester_user_id != approval.requested_by
        or not envelope.session_id
        or envelope.session_id not in {str(session.id), session.external_conv_id}
    ):
        return None
    return approval


def session_approval_projection(approval, *, agent, user):
    from app.services.approval_service import _can_resolve_agent_approval

    details = approval.details or {}
    return {
        "id": str(approval.id), "status": approval.status, "action_type": approval.action_type,
        "tool_name": approval.tool_name, "execution_status": approval.execution_status,
        "details": {key: details[key] for key in ("reason", "summary", "command", "attachment_count") if key in details},
        "execution_receipt": {"continuation_status": (approval.execution_receipt or {}).get("continuation_status")},
        "can_resolve": approval.status == "pending" and _can_resolve_agent_approval(agent, user),
    }


async def invocation_enterprise_approval(db, *, invocation, agent_id, decision, input_hash):
    from app.models.chat_session import ChatSession

    try:
        approval_id = uuid.UUID(str(decision.get("approval_id") or ""))
    except ValueError:
        return None
    session = await db.get(ChatSession, invocation.session_id)
    if session is None or session.tenant_id != invocation.tenant_id or session.agent_id != agent_id:
        return None
    approval = await load_session_approval(db, session=session, approval_id=approval_id)
    if approval is None or approval.tool_name != invocation.tool_name or approval.input_hash != input_hash:
        return None
    envelope = approval.execution_envelope
    if (
        str(envelope.get("runtime_task_id") or "") != str(invocation.run_id)
        or str(envelope.get("tool_call_id") or "") != invocation.provider_tool_use_id
        or approval.decision_id != decision.get("decision_id")
    ):
        return None
    return approval


async def recover_enterprise_permission_waits(db, *, limit=500):
    """Repair the old enterprise-as-local wait; the ticket still owns every effect."""
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services.chat_transcript import lock_transcript_session
    from app.services.session_permission_runtime import (
        _quarantine_same_run_for_reconciliation,
        _queue_same_run_continuation,
        _settle_unstarted_batch_siblings,
        _source_result,
    )
    from app.services.session_tool_runtime import complete_tool_invocation

    candidates = (await db.execute(select(SessionToolInvocation.id, SessionToolInvocation.session_id).join(
        ApprovalRequest, (ApprovalRequest.id == SessionToolInvocation.permission_item_id)
        & (ApprovalRequest.tenant_id == SessionToolInvocation.tenant_id),
    ).join(RuntimeTask, RuntimeTask.id == SessionToolInvocation.run_id).where(
        RuntimeTask.status == "suspended", RuntimeTask.tenant_id == ApprovalRequest.tenant_id,
        RuntimeTask.parent_agent_id == ApprovalRequest.agent_id, RuntimeTask.root_user_id == ApprovalRequest.requested_by,
        SessionToolInvocation.permission_state == "waiting",
        SessionToolInvocation.effect_state == "prepared_not_started", SessionToolInvocation.result_event_id.is_(None),
    ).order_by(SessionToolInvocation.id).limit(max(1, int(limit))))).all()
    recovered = 0
    for invocation_id, session_id in candidates:
        try:
            await lock_transcript_session(db, session_id=session_id)
            invocation = await db.scalar(select(SessionToolInvocation).where(
                SessionToolInvocation.id == invocation_id, SessionToolInvocation.permission_state == "waiting",
                SessionToolInvocation.effect_state == "prepared_not_started", SessionToolInvocation.result_event_id.is_(None),
            ).with_for_update(skip_locked=True))
            if invocation is None:
                await db.rollback()
                continue
            task = await db.get(RuntimeTask, invocation.run_id)
            if task is None or task.status != "suspended" or task.parent_agent_id is None:
                await db.rollback()
                continue
            approval = await db.get(ApprovalRequest, invocation.permission_item_id)
            decision = {"outcome": "require_approval", "approval_id": str(approval.id),
                "decision_id": approval.decision_id, "input_hash": invocation.effective_args_hash}
            bound = await invocation_enterprise_approval(db, invocation=invocation, agent_id=task.parent_agent_id,
                decision=decision, input_hash=invocation.effective_args_hash)
            if bound is None or task.root_user_id != bound.requested_by:
                await db.rollback()
                continue
            await complete_tool_invocation(db, tenant_id=invocation.tenant_id, agent_id=task.parent_agent_id,
                session_id=session_id, invocation_id=invocation.id,
                provider_result_content=json.dumps({"status": "approval_required", "approval_id": str(bound.id),
                    "tool_name": bound.tool_name, "execution_status": bound.execution_status,
                    "recovered_from": "enterprise_permission_wait"}, sort_keys=True),
                execution_evidence={"schema": "hive.tool_execution_evidence.v1", "tool_decision": decision},
                effective_arguments=invocation.effective_arguments_json)
            try:
                source, batch_settled = await _settle_unstarted_batch_siblings(db, tenant_id=invocation.tenant_id,
                    agent_id=task.parent_agent_id, session_id=session_id, invocation=invocation)
            except RuntimeError as exc:
                if str(exc) != "permission_batch_has_uncertain_effect":
                    raise
                uncertain = await db.scalar(select(SessionToolInvocation).where(
                    SessionToolInvocation.tenant_id == invocation.tenant_id, SessionToolInvocation.session_id == session_id,
                    SessionToolInvocation.run_id == invocation.run_id,
                    SessionToolInvocation.provider_request_id == invocation.provider_request_id,
                    SessionToolInvocation.result_event_id.is_(None),
                    SessionToolInvocation.effect_state.in_(("effect_started", "needs_reconciliation")),
                ).order_by(SessionToolInvocation.id).limit(1))
                if uncertain is None:
                    raise
                source = await _source_result(db, invocation)
                await _quarantine_same_run_for_reconciliation(db, tenant_id=invocation.tenant_id,
                    agent_id=task.parent_agent_id, session_id=session_id, invocation=uncertain,
                    source_result=source, reason_code="enterprise_permission_batch_has_uncertain_effect")
                batch_settled = False
            if batch_settled:
                await _queue_same_run_continuation(db, tenant_id=invocation.tenant_id, agent_id=task.parent_agent_id,
                    session_id=session_id, invocation=invocation, source_result=source)
            await db.commit()
            recovered += 1
        except Exception:
            await db.rollback()
            logger.exception("Enterprise permission wait recovery failed: invocation_id=%s", invocation_id)
    return recovered
