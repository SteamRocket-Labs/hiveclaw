"""Single-command shell escalation (Codex-style one-shot privilege elevation).

When a shell command is rejected by the execpolicy / governance gate, the agent
may request a one-time human approval for that *exact* command. This reuses the
existing approval infrastructure rather than building a parallel grant system:

* the request creates an ``ApprovalRequest`` via :mod:`app.services.approval_service`
  with ``details.tool = "run_command"`` and ``details.args.command`` = the exact
  command, so when a human approves it the existing post-approval executor runs
  that command exactly once (governance preflight is the approval decision, so it
  is skipped for that one replay);
* ``origin.type = "command_escalation"`` keeps it OUT of the session-tool set, so
  it is resolved as an admin/enterprise approval (there is an approver), not a
  silent in-session permission;
* nothing durable is granted — there is no standing "this command is allowed"
  record, so a later re-execution hits the gate again and requires a fresh
  escalation request.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.services.approval_service import approval_service

# Reuses the enterprise (admin-resolvable) approval lane. The action_type is the
# capability the approver is granting for this one command.
COMMAND_ESCALATION_ACTION_TYPE = "workspace.command.escalation"
# Deliberately NOT one of enterprise_approval_visibility.SESSION_TOOL_APPROVAL_ORIGIN_TYPES
# ({"agent_session", "approval_request"}); an escalation needs a real approver, so
# it must remain visible to and resolvable through the enterprise approval surface.
COMMAND_ESCALATION_ORIGIN_TYPE = "command_escalation"
_ESCALATION_TOOL = "run_command"


def build_command_escalation_request(
    command: str,
    reason: str | None,
    *,
    requested_by: uuid.UUID | str | None,
    session_id: str | None,
) -> tuple[str, dict[str, Any]]:
    """Build the ``(action_type, details)`` for a one-shot command escalation.

    Pure — no I/O. ``details`` carries the exact command so the post-approval
    executor replays ``run_command`` with it, and ``origin.type`` marks it as an
    admin-resolvable enterprise approval.
    """
    normalized_command = command.strip()
    details: dict[str, Any] = {
        "tool": _ESCALATION_TOOL,
        "args": {"command": normalized_command},
        "command": normalized_command,
        "escalation": True,
        "reason": (reason or "").strip() or "one-time shell command escalation",
        "requested_by": str(requested_by) if requested_by else None,
        "session_id": session_id,
        "origin": {"type": COMMAND_ESCALATION_ORIGIN_TYPE, "session_id": session_id},
    }
    return COMMAND_ESCALATION_ACTION_TYPE, details


async def request_command_escalation(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    requested_by: uuid.UUID | str | None,
    command: str,
    reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Create a pending one-shot escalation approval for ``command``.

    Returns the approval outcome (``allowed`` is always False here — approval is
    pending until a human resolves it). Raises no persistent grant; each call is a
    fresh single-use request.
    """
    normalized_command = command.strip()
    if not normalized_command:
        return {"allowed": False, "error": "command is required for escalation."}
    tenant_uuid = _normalize_uuid(tenant_id)
    if tenant_uuid is None:
        return {"allowed": False, "error": "tenant_id is required for escalation."}

    action_type, details = build_command_escalation_request(
        normalized_command,
        reason,
        requested_by=requested_by,
        session_id=session_id,
    )

    async with tenant_scoped_session(
        tenant_uuid,
        require_tenant=True,
        source="command_escalation_request",
    ) as db:
        result = await db.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.tenant_id == tenant_uuid,
                Agent.deleted_at.is_(None),
            )
        )
        agent = result.scalar_one_or_none()
        if agent is None:
            return {"allowed": False, "error": "Agent not found for escalation."}
        outcome = await approval_service.request_approval(db, agent, action_type=action_type, details=details)
        return outcome


def _normalize_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None
