from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_tool_runtime_resolver_builds_execution_context(monkeypatch):
    from app.core.execution_context import ExecutionIdentity
    from app.tools.resolver import ToolRuntimeResolver

    agent_id = uuid4()
    user_id = uuid4()
    workspace = Path("/tmp/agent-ws")

    class _FakeScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalarResult(uuid4())

    async def fake_ensure_workspace(_agent_id, tenant_id=None):
        assert _agent_id == agent_id
        assert tenant_id is not None
        return workspace

    monkeypatch.setattr("app.tools.resolver.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.tools.resolver.ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr(
        "app.tools.resolver.get_execution_identity",
        lambda: ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=user_id,
            label="Rocky via web",
        ),
    )

    resolver = ToolRuntimeResolver()
    context = await resolver.resolve(agent_id=agent_id, user_id=user_id)

    assert context.agent_id == agent_id
    assert context.user_id == user_id
    assert context.workspace == workspace
    assert context.tenant_id is not None
    assert context.execution_identity is not None
    assert context.execution_identity.identity_type == "delegated_user"
    assert context.execution_identity.identity_id == user_id


@pytest.mark.asyncio
async def test_tool_runtime_resolver_tolerates_missing_tenant(monkeypatch):
    from app.tools.resolver import ToolRuntimeResolver

    agent_id = uuid4()
    user_id = uuid4()
    workspace = Path("/tmp/agent-ws")

    class _FakeScalarResult:
        def scalar_one_or_none(self):
            return None

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalarResult()

    async def fake_ensure_workspace(_agent_id, tenant_id=None):
        assert tenant_id is None
        return workspace

    monkeypatch.setattr("app.tools.resolver.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.tools.resolver.ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr("app.tools.resolver.get_execution_identity", lambda: None)

    resolver = ToolRuntimeResolver()
    context = await resolver.resolve(agent_id=agent_id, user_id=user_id)

    assert context.tenant_id is None
    assert context.execution_identity is None
    assert context.workspace == workspace
