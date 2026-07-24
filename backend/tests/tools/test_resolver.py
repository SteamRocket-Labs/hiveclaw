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
    tenant_id = uuid4()
    workspace = Path("/tmp/agent-ws")

    async def fake_resolve_tenant_for_agent(_agent_id):
        assert _agent_id == agent_id
        return tenant_id

    async def fake_ensure_workspace(_agent_id, tenant_id=None):
        assert _agent_id == agent_id
        assert tenant_id is not None
        return workspace

    monkeypatch.setattr("app.tools.resolver.resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr("app.tools.resolver.ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr(
        "app.tools.resolver.get_execution_identity",
        lambda: ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=user_id,
            label="Rocky via web",
        ),
    )

    authority_scope = object()
    action_policy = object()
    from app.services.exact_secret_boundary import ExactSecretBoundary

    secret_boundary = ExactSecretBoundary.from_pairs(
        (("tool-config://tenant/search/api_key", "sk-live-resolver-secret-0123456789"),)
    )

    async def fake_workspace_authority_loader(**kwargs):
        assert kwargs == {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "session_id": None,
        }
        return authority_scope

    async def fake_owner_action_policy_loader(**kwargs):
        assert kwargs == {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
        }
        return action_policy

    async def fake_secret_boundary_loader(**kwargs):
        assert kwargs == {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
        }
        return secret_boundary

    resolver = ToolRuntimeResolver(
        workspace_authority_loader=fake_workspace_authority_loader,
        owner_action_policy_loader=fake_owner_action_policy_loader,
        secret_boundary_loader=fake_secret_boundary_loader,
    )
    context = await resolver.resolve(agent_id=agent_id, user_id=user_id)

    assert context.agent_id == agent_id
    assert context.user_id == user_id
    assert context.workspace == workspace
    assert context.tenant_id == str(tenant_id)
    assert context.execution_identity is not None
    assert context.execution_identity.identity_type == "delegated_user"
    assert context.execution_identity.identity_id == user_id
    assert context.workspace_authority_scope is authority_scope
    assert context.owner_action_policy is action_policy
    assert context.exact_secret_boundary is secret_boundary


@pytest.mark.asyncio
async def test_tool_runtime_resolver_degrades_policy_dependency_without_blocking_read_only_tools(monkeypatch):
    from app.services.action_preflight import CharterZone
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
    )
    from app.tools.resolver import ToolRuntimeResolver

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    async def fake_ensure_workspace(_agent_id, tenant_id=None):
        return Path("/tmp/agent-ws")

    async def fake_workspace_authority_loader(**_kwargs):
        return object()

    async def unavailable_policy_loader(**_kwargs):
        raise RuntimeError("policy database unavailable")

    async def empty_secret_boundary_loader(**_kwargs):
        from app.services.exact_secret_boundary import ExactSecretBoundary

        return ExactSecretBoundary.empty()

    monkeypatch.setattr("app.tools.resolver.resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr("app.tools.resolver.ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr("app.tools.resolver.get_execution_identity", lambda: None)

    context = await ToolRuntimeResolver(
        workspace_authority_loader=fake_workspace_authority_loader,
        owner_action_policy_loader=unavailable_policy_loader,
        secret_boundary_loader=empty_secret_boundary_loader,
    ).resolve(agent_id=agent_id, user_id=user_id)

    assert context.owner_action_policy.valid is False
    assert context.owner_action_policy.error_code == "policy_dependency_unavailable"
    assert context.owner_action_policy.zone_for(ACTION_EXTERNAL_EFFECT) == CharterZone.NEVER_DO
    assert context.owner_action_policy.zone_for(ACTION_LOCAL_READ) == CharterZone.FULL_AUTHORITY
    assert context.owner_action_policy.zone_for(ACTION_LOCAL_WRITE) == CharterZone.NEVER_DO


@pytest.mark.asyncio
async def test_tool_runtime_resolver_fails_closed_when_secret_authority_is_unavailable(monkeypatch):
    from app.runtime.tenant_admission import RuntimeTenantPreconditionError
    from app.tools.resolver import ToolRuntimeResolver

    agent_id = uuid4()
    tenant_id = uuid4()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr("app.tools.resolver.resolve_tenant_for_agent", fake_resolve_tenant_for_agent)

    async def fake_ensure_workspace(_agent_id, tenant_id=None):
        return Path("/tmp/agent-ws")

    async def fake_workspace_authority_loader(**_kwargs):
        return object()

    async def fake_owner_action_policy_loader(**_kwargs):
        return object()

    async def unavailable_secret_boundary_loader(**_kwargs):
        raise RuntimeError("credential database unavailable")

    monkeypatch.setattr("app.tools.resolver.ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr("app.tools.resolver.get_execution_identity", lambda: None)

    resolver = ToolRuntimeResolver(
        workspace_authority_loader=fake_workspace_authority_loader,
        owner_action_policy_loader=fake_owner_action_policy_loader,
        secret_boundary_loader=unavailable_secret_boundary_loader,
    )
    with pytest.raises(RuntimeTenantPreconditionError) as exc:
        await resolver.resolve(agent_id=agent_id, user_id=uuid4())

    assert exc.value.reason_code == "credential_authority_unavailable"
    assert "credential database unavailable" not in str(exc.value)


@pytest.mark.asyncio
async def test_tool_runtime_resolver_blocks_missing_tenant(monkeypatch):
    from app.runtime.tenant_admission import RuntimeTenantPreconditionError
    from app.tools.resolver import ToolRuntimeResolver

    agent_id = uuid4()
    user_id = uuid4()
    workspace_opened = False

    async def fake_resolve_tenant_for_agent(_agent_id):
        return None

    async def fake_ensure_workspace(_agent_id, tenant_id=None):
        nonlocal workspace_opened
        workspace_opened = True
        raise AssertionError("workspace should not be opened when tenant admission blocks")

    monkeypatch.setattr("app.tools.resolver.resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr("app.tools.resolver.ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr("app.tools.resolver.get_execution_identity", lambda: None)

    resolver = ToolRuntimeResolver()
    with pytest.raises(RuntimeTenantPreconditionError) as exc:
        await resolver.resolve(agent_id=agent_id, user_id=user_id)

    assert workspace_opened is False
    assert exc.value.status == "blocked_precondition"
    assert exc.value.reason_code == "agent_tenant_missing"
