"""Tests for the single-command shell escalation flow (D2).

Covers the full acceptance loop — reject → request → approve → run once →
re-execution still needs a fresh request — plus the reuse of the existing
approval infrastructure (no parallel grant system, no persistent standing grant).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.command_escalation_service import (
    COMMAND_ESCALATION_ACTION_TYPE,
    COMMAND_ESCALATION_ORIGIN_TYPE,
    build_command_escalation_request,
    request_command_escalation,
)
from app.services.enterprise_approval_visibility import is_session_tool_approval

# A command the execpolicy gate blocks (used as a realistic escalation target).
_BLOCKED_COMMAND = "rm -rf /data/cache"


# ── Pure request builder (unconditional) ──────────────────────────────


def test_build_escalation_request_carries_exact_command_and_admin_lane():
    action_type, details = build_command_escalation_request(
        _BLOCKED_COMMAND, "free disk", requested_by="user-1", session_id="sess-1"
    )
    assert action_type == COMMAND_ESCALATION_ACTION_TYPE
    # The post-approval executor replays run_command with the exact command.
    assert details["tool"] == "run_command"
    assert details["args"]["command"] == _BLOCKED_COMMAND
    assert details["command"] == _BLOCKED_COMMAND  # audit-visible original text
    assert details["escalation"] is True
    assert details["reason"] == "free disk"
    assert details["requested_by"] == "user-1"
    # origin.type keeps it in the admin-resolvable (enterprise) lane.
    assert details["origin"]["type"] == COMMAND_ESCALATION_ORIGIN_TYPE
    assert is_session_tool_approval(SimpleNamespace(details=details)) is False


def test_build_escalation_request_defaults_reason_and_trims_command():
    _, details = build_command_escalation_request("  do-thing --now  ", "", requested_by=None, session_id=None)
    assert details["args"]["command"] == "do-thing --now"
    assert details["reason"] == "one-time shell command escalation"
    assert details["requested_by"] is None


# ── Service glue: creates a pending approval via approval_service ──────


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    def __init__(self, agent):
        self._agent = agent
        self.committed = False

    async def execute(self, _query):
        return _FakeResult(self._agent)

    async def commit(self):
        self.committed = True


class _AsyncCtx:
    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_a):
        return False


def _patch_db(monkeypatch, agent):
    from app.services import command_escalation_service as svc

    db = _FakeDb(agent)
    monkeypatch.setattr(svc, "async_session", lambda: _AsyncCtx(db))
    monkeypatch.setattr(svc, "enter_rls_bypass", lambda _db, reason=None: _AsyncCtx(None))
    return db


@pytest.mark.asyncio
async def test_request_command_escalation_creates_pending_approval(monkeypatch):
    from app.services import command_escalation_service as svc

    agent_id = uuid4()
    user_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4(), name="tester", creator_id=uuid4())
    _patch_db(monkeypatch, agent)

    recorded = {}

    async def fake_request_approval(db, passed_agent, *, action_type, details):
        recorded["action_type"] = action_type
        recorded["details"] = details
        recorded["agent"] = passed_agent
        return {"allowed": False, "approval_id": "appr-1", "message": "Approval requested from creator"}

    monkeypatch.setattr(svc.approval_service, "request_approval", fake_request_approval)

    outcome = await request_command_escalation(
        agent_id=agent_id, requested_by=user_id, command=_BLOCKED_COMMAND, reason="cleanup", session_id="sess-9"
    )

    assert outcome["approval_id"] == "appr-1"
    assert outcome["allowed"] is False  # pending until a human resolves it
    assert recorded["agent"] is agent
    assert recorded["action_type"] == COMMAND_ESCALATION_ACTION_TYPE
    assert recorded["details"]["args"]["command"] == _BLOCKED_COMMAND
    assert recorded["details"]["requested_by"] == str(user_id)


@pytest.mark.asyncio
async def test_request_command_escalation_rejects_empty_command():
    outcome = await request_command_escalation(agent_id=uuid4(), requested_by=uuid4(), command="   ")
    assert outcome["allowed"] is False
    assert "required" in outcome["error"]


@pytest.mark.asyncio
async def test_request_command_escalation_handles_missing_agent(monkeypatch):
    from app.services import command_escalation_service as svc

    _patch_db(monkeypatch, None)  # agent lookup returns None

    async def _boom(*_a, **_kw):
        raise AssertionError("approval must not be requested when the agent is missing")

    monkeypatch.setattr(svc.approval_service, "request_approval", _boom)
    outcome = await request_command_escalation(agent_id=uuid4(), requested_by=uuid4(), command=_BLOCKED_COMMAND)
    assert outcome["allowed"] is False
    assert "Agent not found" in outcome["error"]


# ── Full acceptance loop: reject → request → approve→run-once → re-request


def _governance_deps():
    from app.tools.governance import GovernanceDependencies

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, _tool_name):
        return SimpleNamespace(denied=False, escalate_to_l3=False, capability="workspace.command.execute", reason="")

    async def write_audit(**_kwargs):
        return None

    async def request_approval(*_a, **_kw):
        raise AssertionError("dangerous-command denial stays inside the session, not enterprise approval")

    return GovernanceDependencies(
        resolve_security_zone=resolve_security_zone,
        check_capability=check_capability,
        write_audit_event=write_audit,
        request_approval=request_approval,
    )


async def _governance_denies(command: str) -> str | None:
    from app.tools.governance import ToolGovernanceContext, run_tool_governance

    return await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="run_command",
            arguments={"command": command},
        ),
        _governance_deps(),
    )


@pytest.mark.asyncio
async def test_full_escalation_loop_reject_approve_runonce_then_rerequest(monkeypatch):
    from app.services.approval_service import approval_service

    # 1. REJECT — the gate blocks the dangerous command.
    denial = await _governance_denies(_BLOCKED_COMMAND)
    assert denial is not None
    assert "requires session permission" in denial

    # 2. REQUEST — build the escalation approval for that exact command.
    action_type, details = build_command_escalation_request(
        _BLOCKED_COMMAND, "cleanup", requested_by=uuid4(), session_id="sess-1"
    )

    # 3. APPROVE → RUN ONCE — the post-approval executor replays run_command
    #    with the exact command exactly once (governance preflight is skipped
    #    because the human approval IS the governance decision).
    calls: list[tuple[str, dict]] = []

    async def fake_execute_approved_tool(tool_name, arguments, agent_id, **_kw):
        calls.append((tool_name, dict(arguments)))
        return "executed"

    monkeypatch.setattr("app.services.agent_tools.execute_approved_tool", fake_execute_approved_tool)

    approver = uuid4()
    result = await approval_service._execute_approved_action(
        uuid4(), action_type, details, approved_by_user_id=approver, approval_id=uuid4()
    )
    assert result == "executed"
    assert calls == [("run_command", {"command": _BLOCKED_COMMAND})]  # exact command, exactly once

    # 4. RE-EXECUTION still needs a fresh request — nothing durable was granted,
    #    so the gate blocks the same command again.
    denial_again = await _governance_denies(_BLOCKED_COMMAND)
    assert denial_again is not None
    assert "requires session permission" in denial_again


# ── Tool handler wires command + reason into the escalation service ────


@pytest.mark.asyncio
async def test_request_shell_escalation_tool_wires_service(monkeypatch):
    from app.tools.handlers import command_parity

    captured = {}

    async def fake_service(*, agent_id, requested_by, command, reason, session_id):
        captured.update(agent_id=agent_id, command=command, reason=reason, session_id=session_id)
        return {"allowed": False, "approval_id": "appr-42"}

    monkeypatch.setattr(command_parity, "request_command_escalation", fake_service)

    request = SimpleNamespace(
        context=SimpleNamespace(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), session_id="sess-1"),
        arguments={"command": _BLOCKED_COMMAND, "reason": "cleanup"},
    )
    out = await command_parity.request_shell_escalation(request)
    payload = json.loads(out)

    assert payload["ok"] is True
    assert payload["status"] == "approval_required"
    assert payload["command"] == _BLOCKED_COMMAND
    assert payload["escalation"]["approval_id"] == "appr-42"
    assert captured["command"] == _BLOCKED_COMMAND
    assert captured["reason"] == "cleanup"


@pytest.mark.asyncio
async def test_request_shell_escalation_tool_requires_command():
    from app.tools.handlers import command_parity

    request = SimpleNamespace(
        context=SimpleNamespace(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), session_id="s"),
        arguments={"command": "   "},
    )
    payload = json.loads(await command_parity.request_shell_escalation(request))
    assert payload["ok"] is False
    assert "required" in payload["error"]
