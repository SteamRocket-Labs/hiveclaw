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
        delegation_token="token-1",
    )

    assert context.agent_id == agent_id
    assert context.user_id == user_id
    assert context.tenant_id == runtime_context.tenant_id
    assert context.tool_name == "write_file"
    assert context.arguments == {"path": "focus.md", "content": "x"}
    assert context.delegation_token == "token-1"


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
    monkeypatch.setattr("app.tools.governance_resolver.tenant_scoped_session", lambda *a, **k: fake_session)
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
        session_id="session-approval",
    )
    assert result == {"allowed": False, "approval_id": "approval-1"}
    assert approval_calls[0][2] == "workspace.write"
    assert approval_calls[0][3]["tool"] == "write_file"
    assert approval_calls[0][3]["args"] == {"path": "focus.md"}
    assert approval_calls[0][3]["reason"] == "manual escalation"
    assert approval_calls[0][3]["session_id"] == "session-approval"


@pytest.mark.asyncio
async def test_resolver_mcp_mode_unwraps_target_and_fast_paths(monkeypatch):
    """Closure A2: the resolver feeds the governance MCP gate.

    call_mcp_tool is the generic entry — the governed object is the target
    tool inside its arguments; dynamic MCP tool names govern themselves;
    a name that is not an MCP Tool row returns None without touching the
    per-agent mode resolution (fast path for every ordinary tool call).
    """
    from app.tools.governance_resolver import ToolGovernanceResolver

    agent_id = uuid4()
    mcp_tool_row = SimpleNamespace(id=uuid4(), type="mcp", name="notion_search")
    mode_calls = []

    class _FakeScalar:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeSession:
        def __init__(self, row):
            self._row = row

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalar(self._row)

    async def fake_resolve_agent_mcp_tool_mode(db, aid, tool):
        mode_calls.append((aid, tool.name))
        return "approval"

    monkeypatch.setattr(
        "app.services.mcp_server_service.resolve_agent_mcp_tool_mode",
        fake_resolve_agent_mcp_tool_mode,
    )

    resolver = ToolGovernanceResolver()

    # 1. call_mcp_tool unwraps the target from arguments
    monkeypatch.setattr("app.tools.governance_resolver.async_session", lambda: _FakeSession(mcp_tool_row))
    deps = resolver.build_dependencies()
    assert deps.resolve_mcp_tool_mode is not None
    mode = await deps.resolve_mcp_tool_mode(agent_id, "call_mcp_tool", {"tool_name": "notion_search"})
    assert mode == "approval"
    assert mode_calls == [(agent_id, "notion_search")]

    # 2. dynamic MCP tool name governs itself
    mode = await deps.resolve_mcp_tool_mode(agent_id, "notion_search", {})
    assert mode == "approval"

    # 3. not an MCP Tool row → None fast path, mode resolution untouched
    mode_calls.clear()
    monkeypatch.setattr("app.tools.governance_resolver.async_session", lambda: _FakeSession(None))
    deps = resolver.build_dependencies()
    assert await deps.resolve_mcp_tool_mode(agent_id, "read_file", {"path": "x"}) is None
    assert mode_calls == []

    # 4. call_mcp_tool without a target name → None (validation happens handler-side)
    assert await deps.resolve_mcp_tool_mode(agent_id, "call_mcp_tool", {}) is None


@pytest.mark.asyncio
async def test_resolver_mcp_mode_lookup_is_scoped_to_enabled_agent_assignment(monkeypatch):
    """Closure A2 review-fix: tool names are only tenant-unique.

    The governance resolver must resolve the MCP mode for the concrete tool
    assigned to this agent, not the first global Tool row with the same name.
    """
    from app.tools.governance_resolver import ToolGovernanceResolver

    agent_id = uuid4()
    mcp_tool_row = SimpleNamespace(id=uuid4(), type="mcp", name="notion_search")

    class _FakeScalar:
        def scalar_one_or_none(self):
            return mcp_tool_row

    class _FakeSession:
        def __init__(self):
            self.executed_statements = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, query):
            self.executed_statements.append(query)
            return _FakeScalar()

    async def fake_resolve_agent_mcp_tool_mode(db, aid, tool):
        assert aid == agent_id
        assert tool is mcp_tool_row
        return "approval"

    session = _FakeSession()
    monkeypatch.setattr("app.tools.governance_resolver.async_session", lambda: session)
    monkeypatch.setattr(
        "app.services.mcp_server_service.resolve_agent_mcp_tool_mode",
        fake_resolve_agent_mcp_tool_mode,
    )

    deps = ToolGovernanceResolver().build_dependencies()
    assert deps.resolve_mcp_tool_mode is not None
    assert await deps.resolve_mcp_tool_mode(agent_id, "call_mcp_tool", {"tool_name": "notion_search"}) == "approval"

    # enter_rls_bypass also records SET LOCAL app.current_tenant_id statements —
    # pick the business query, not the GUC set.
    business_sql = [
        str(s.compile(compile_kwargs={"literal_binds": False})).lower()
        for s in session.executed_statements
        if "app.current_tenant_id" not in str(s).lower()
    ]
    sql = business_sql[0]
    assert "join agent_tools" in sql
    assert "agent_tools.agent_id" in sql
    assert "agent_tools.enabled" in sql
    assert "tools.enabled" in sql
