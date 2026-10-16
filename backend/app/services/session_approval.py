"""Read an enterprise tool ticket through its immutable originating-session binding."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.audit import ApprovalRequest
from app.services.approval_ticket import ApprovalTicketError, restore_approval_execution_envelope
from app.services.enterprise_approval_visibility import is_visible_enterprise_approval


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
    if str(envelope.get("runtime_task_id") or "") != str(invocation.run_id):
        return None
    return approval
