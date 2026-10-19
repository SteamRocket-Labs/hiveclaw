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
        or (envelope.machine_credential_id is not None and (session.user_id is not None
            or session.external_principal_id != envelope.requester_external_principal_id
            or approval.requested_by_external_principal_id != envelope.requester_external_principal_id))
        or not envelope.session_id
        or envelope.session_id not in {str(session.id), session.external_conv_id}
    ):
        return None
    return approval


async def session_approval_projection(approval, *, db, agent, user):
    from app.services.approval_service import can_resolve_approval

    details = {key: value for key, value in (approval.details or {}).items()
        if key in {"reason", "summary", "command", "attachment_count"}}
    if approval.tool_name in {"write_file", "fs_write"}:
        path = (approval.normalized_arguments or {}).get("path")
        if isinstance(path, str):
            details["path"] = path
    return {
        "id": str(approval.id), "status": approval.status, "action_type": approval.action_type,
        "agent_id": str(agent.id), "agent_name": agent.name, "created_at": approval.created_at,
        "resolved_at": approval.resolved_at, "resolved_by": approval.resolved_by,
        "tool_name": approval.tool_name, "execution_status": approval.execution_status,
        "details": details,
        "execution_receipt": {"continuation_status": (approval.execution_receipt or {}).get("continuation_status")},
        "can_resolve": approval.status == "pending" and await can_resolve_approval(db, agent=agent, user=user, approval=approval),
    }


async def invocation_enterprise_approval(db, *, invocation, agent_id, decision, input_hash):
    from app.models.chat_session import ChatSession
    from app.services.approval_ticket import hash_tool_input

    try:
        approval_id = uuid.UUID(str(decision.get("approval_id") or ""))
    except ValueError:
        return None
    session = await db.get(ChatSession, invocation.session_id)
    if session is None or session.tenant_id != invocation.tenant_id or session.agent_id != agent_id:
        return None
    approval = await load_session_approval(db, session=session, approval_id=approval_id)
    if (approval is None or approval.tool_name != invocation.tool_name or approval.input_hash != input_hash
        or not isinstance(approval.normalized_arguments, dict)
        or hash_tool_input(approval.tool_name, approval.normalized_arguments) != approval.input_hash):
        return None
    envelope = approval.execution_envelope
    try:
        same_run = uuid.UUID(str(envelope.get("runtime_task_id") or "")) == invocation.run_id
    except ValueError:
        same_run = False
    if (
        not same_run
        or str(envelope.get("tool_call_id") or "") != invocation.provider_tool_use_id
        or approval.decision_id != decision.get("decision_id")
    ):
        return None
    return approval


async def group_approval_origin(db, *, session, approval):
    """Bind a company ticket to the original canonical call, never to approver resources."""
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_model_inputs import _hash
    from app.services.session_semantic_history import _event_payload

    try:
        run_id = uuid.UUID(str(approval.execution_envelope.get("runtime_task_id")))
    except (ValueError, TypeError) as exc:
        raise ApprovalTicketError("approval group origin is unavailable") from exc
    invocation = await db.scalar(select(SessionToolInvocation).where(
        SessionToolInvocation.tenant_id == session.tenant_id, SessionToolInvocation.session_id == session.id,
        SessionToolInvocation.run_id == run_id,
        SessionToolInvocation.provider_tool_use_id == approval.execution_envelope.get("tool_call_id")))
    task = await db.get(RuntimeTask, run_id)
    if invocation is None or task is None or invocation.result_event_id is None:
        raise ApprovalTicketError("approval group origin is unavailable")
    bound = await invocation_enterprise_approval(db, invocation=invocation, agent_id=session.agent_id,
        decision={"approval_id": str(approval.id), "decision_id": approval.decision_id}, input_hash=approval.input_hash)
    event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
    payload = _event_payload(event) if event else {}
    try:
        result = json.loads(payload.get("content", ""))
    except (ValueError, TypeError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    if (bound is None or task.root_user_id != approval.requested_by or invocation.tool_name != approval.tool_name
        or invocation.effective_args_hash != _hash({"tool_name": invocation.tool_name,
            "arguments": invocation.effective_arguments_json})
        or event is None or (event.tenant_id, event.agent_id, event.session_id, event.run_id) != (
            session.tenant_id, session.agent_id, session.id, run_id)
        or payload.get("content_hash") != _hash(payload.get("content"))
        or result.get("status") != "approval_required" or result.get("approval_id") != str(approval.id)):
        raise ApprovalTicketError("approval group origin does not match the canonical call")
    return task, invocation, event


async def group_approval_requester(db, *, session, approval):
    from app.services.shared_group_authority import _load_shared_group_task_actor

    task, invocation, _event = await group_approval_origin(db, session=session, approval=approval)
    actor = await _load_shared_group_task_actor(db, task=task, session=session)
    if actor.id != approval.requested_by or not actor.is_active or task.status == "killed":
        raise ApprovalTicketError("approval group requester is no longer authorized")
    return actor, invocation


async def authorize_session_approval_reader(db, *, session, approval, user):
    if getattr(session, "audience_kind", None) != "group":
        return
    from fastapi import HTTPException
    from app.services.shared_group_history import group_event_access

    try:
        _task, _invocation, event = await group_approval_origin(db, session=session, approval=approval)
    except ApprovalTicketError as exc:
        raise HTTPException(503, detail={"code": "group_approval_sources_unavailable"}) from exc
    receipt = (await group_event_access(db, session=session, events=[event], user=user))[event.id]
    if receipt:
        raise HTTPException(403 if receipt["status"] == "denied" else 503, detail=receipt)


async def group_approval_return_actor(db, *, session, rows):
    """A durable approval return preserves its requester's purpose and exact outbox route."""
    requester = None
    if not rows:
        raise PermissionError("group_approval_return_source_missing")
    for row in rows:
        try:
            approval_id = uuid.UUID(str((row.metadata_json or {}).get("approval_id")))
        except (ValueError, TypeError) as exc:
            raise PermissionError("group_approval_return_source_mismatch") from exc
        approval = await load_session_approval(db, session=session, approval_id=approval_id)
        if (approval is None or row.source_kind != "approval" or row.tenant_id != session.tenant_id
            or row.parent_agent_id != session.agent_id or row.parent_session_id != session.id
            or row.parent_user_id != approval.requested_by
            or str(row.source_run_id) != str(approval.execution_task_id or approval.id)
            or (approval.execution_receipt or {}).get("continuation_outbox_id") != str(row.id)):
            raise PermissionError("group_approval_return_source_mismatch")
        actor, _invocation = await group_approval_requester(db, session=session, approval=approval)
        if requester is not None and requester.id != actor.id:
            raise PermissionError("group_approval_return_requester_mismatch")
        requester = actor
    if requester is None or not requester.is_active:
        raise PermissionError("group_approval_return_requester_unavailable")
    return requester


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
