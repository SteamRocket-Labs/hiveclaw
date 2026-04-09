from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_tool_governance_resolver_builds_context_from_runtime_context():
    from app.core.execution_context import ExecutionIdentity
    from app.tools.governance_resolver import ToolGovernanceResolver
    from app.tools.runtime import ToolExecutionContext

    agent_id = uuid4()
    user_id = uuid4()
    runtime_context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id=str(uuid4()),
        workspace=SimpleNamespace(),
        execution_identity=ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=user_id,
            label="Rocky via web",
        ),
    )

    resolver = ToolGovernanceResolver()
    context = await resolver.build_context(
        runtime_context=runtime_context,
        tool_name="write_file",
        arguments={"path": "focus.md", "content": "x"},
    )

    assert context.agent_id == agent_id
    assert context.user_id == user_id
    assert context.tenant_id == runtime_context.tenant_id
    assert context.tool_name == "write_file"
    assert context.arguments == {"path": "focus.md", "content": "x"}


@pytest.mark.asyncio
async def test_tool_governance_resolver_dependencies_wrap_services(monkeypatch):
    from app.tools.governance_resolver import ToolGovernanceResolver

    tenant_id = uuid4()
    agent_id = uuid4()
    audit_calls = []
    capability_calls = []
    approval_calls = []

    class _FakeScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeSession:
        def __init__(self):
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalarResult(SimpleNamespace(security_zone="restricted"))

        async def commit(self):
            self.committed = True

    async def fake_check_capability(db, tenant_uuid, agent_uuid, tool_name):
        capability_calls.append((db, tenant_uuid, agent_uuid, tool_name))
        return SimpleNamespace(denied=False, escalate_to_l3=False, capability="workspace.write", reason="")

    async def fake_write_audit_event(db, **kwargs):
        audit_calls.append((db, kwargs))

    class _FakeApprovalService:
        async def request_approval(self, db, agent, *, action_type, details):
            approval_calls.append((db, agent, action_type, details))
            return {"allowed": False, "approval_id": "approval-1"}

    fake_session = _FakeSession()
    monkeypatch.setattr("app.tools.governance_resolver.async_session", lambda: fake_session)
    monkeypatch.setattr("app.tools.governance_resolver.check_capability", fake_check_capability)
    monkeypatch.setattr("app.tools.governance_resolver.write_audit_event", fake_write_audit_event)
    monkeypatch.setattr("app.tools.governance_resolver.approval_service", _FakeApprovalService())

    resolver = ToolGovernanceResolver()
    deps = resolver.build_dependencies()

    assert await deps.resolve_security_zone(agent_id) == "restricted"

    cap_result = await deps.check_capability(tenant_id, agent_id, "write_file")
    assert cap_result.capability == "workspace.write"
    assert capability_calls[0][1:] == (tenant_id, agent_id, "write_file")

    await deps.write_audit_event(event_type="capability.denied", tenant_id=tenant_id)
    assert audit_calls[0][1]["event_type"] == "capability.denied"
    assert fake_session.committed is True

    result = await deps.request_approval(
        agent_id=agent_id,
        user_id=uuid4(),
        tool_name="write_file",
        arguments={"path": "focus.md"},
        capability="workspace.write",
        reason="manual escalation",
    )
    assert result == {"allowed": False, "approval_id": "approval-1"}
    assert approval_calls[0][2] == "workspace.write"
    assert approval_calls[0][3]["tool"] == "write_file"
    assert approval_calls[0][3]["args"] == {"path": "focus.md"}
    assert approval_calls[0][3]["reason"] == "manual escalation"
