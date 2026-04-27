from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def test_governance_uses_canonical_approval_dependency_without_compat_wrapper() -> None:
    source = (APP_ROOT / "tools/governance.py").read_text(encoding="utf-8")

    assert "async def _request_approval_compat" not in source
    assert "_request_approval_compat(" not in source
    assert "deps.request_approval(" in source
    assert "reason=dangerous_reason" in source


@pytest.mark.asyncio
async def test_secret_exfiltration_command_requires_approval() -> None:
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    approval_calls = []
    events = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, _tool_name):
        return SimpleNamespace(denied=False, escalate_to_l3=False, capability=None, reason="")

    async def write_audit_event(**_kwargs):
        return None

    async def request_approval(*, agent_id, user_id, tool_name, arguments, capability, reason=None):
        approval_calls.append(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "capability": capability,
                "reason": reason,
            }
        )
        return {"allowed": False, "approval_id": "approval-secret"}

    agent_id = uuid4()
    user_id = uuid4()
    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=str(uuid4()),
            tool_name="run_command",
            arguments={"command": "cat .env && printenv SECRET_KEY"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit_event,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert "approval-secret" in str(message)
    assert approval_calls == [
        {
            "agent_id": agent_id,
            "user_id": user_id,
            "tool_name": "run_command",
            "arguments": {"command": "cat .env && printenv SECRET_KEY"},
            "capability": "workspace.command.secret_exfiltration",
            "reason": "secret exfiltration",
        }
    ]
    assert events[-1]["status"] == "approval_required"
    assert events[-1]["capability"] == "workspace.command.secret_exfiltration"
