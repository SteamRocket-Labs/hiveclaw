"""PDEC-013 Local Bridge pairing approval: administrator actor vs host owner.

An approving scoped administrator binds the device connection to the Agent's
host owner (the employee whose runner and workspace consume it) and is
recorded as the audited approval actor. A legacy ``manage`` grantee is not
administrator identity and keeps binding to itself — it can never mint an
owner-bound daemon credential for someone else's Agent.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _world():
    tenant_id = uuid4()
    employee_id = uuid4()
    admin_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, owner_user_id=employee_id)
    return tenant_id, employee_id, admin_id, agent


@pytest.mark.asyncio
async def test_org_admin_approval_binds_host_owner_with_audited_actor(monkeypatch):
    import app.api.local_bridge as local_bridge_api

    tenant_id, employee_id, admin_id, agent = _world()
    admin = SimpleNamespace(id=admin_id, role="org_admin", tenant_id=tenant_id)
    captured = {}

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_approve(_db, *, user_code, user_id, tenant_id, agent_id, metadata):
        captured.update(user_code=user_code, user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, metadata=metadata)
        return {"status": "approved", "user_id": str(user_id), "agent_id": str(agent_id), "tenant_id": str(tenant_id)}

    monkeypatch.setattr(local_bridge_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(local_bridge_api.bridge_service, "approve_pairing_session", fake_approve)

    result = await local_bridge_api.approve_agent_bridge_pairing(
        agent_id=agent.id,
        user_code="CODE-1",
        current_user=admin,
        db=object(),
    )

    assert str(captured["user_id"]) == str(employee_id)
    assert str(captured["agent_id"]) == str(agent.id)
    assert captured["metadata"]["approval_actor_user_id"] == str(admin_id)
    assert captured["metadata"]["approval_actor_role"] == "org_admin"
    assert captured["metadata"]["binding_user_id"] == str(employee_id)
    assert result["user_id"] == str(employee_id)


@pytest.mark.asyncio
async def test_owner_approval_keeps_binding_to_itself(monkeypatch):
    import app.api.local_bridge as local_bridge_api

    tenant_id, employee_id, _admin_id, agent = _world()
    owner = SimpleNamespace(id=employee_id, role="member", tenant_id=tenant_id)
    captured = {}

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_approve(_db, *, user_id, metadata, **_kwargs):
        captured.update(user_id=user_id, metadata=metadata)
        return {"status": "approved"}

    monkeypatch.setattr(local_bridge_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(local_bridge_api.bridge_service, "approve_pairing_session", fake_approve)

    await local_bridge_api.approve_agent_bridge_pairing(
        agent_id=agent.id,
        user_code="CODE-2",
        current_user=owner,
        db=object(),
    )

    assert str(captured["user_id"]) == str(employee_id)
    assert "approval_actor_user_id" not in captured["metadata"]


@pytest.mark.asyncio
async def test_legacy_manage_grantee_binds_itself_not_the_owner(monkeypatch):
    import app.api.local_bridge as local_bridge_api

    tenant_id, employee_id, _admin_id, agent = _world()
    grantee = SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id)
    captured = {}

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_approve(_db, *, user_id, metadata, **_kwargs):
        captured.update(user_id=user_id, metadata=metadata)
        return {"status": "approved"}

    monkeypatch.setattr(local_bridge_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(local_bridge_api.bridge_service, "approve_pairing_session", fake_approve)

    await local_bridge_api.approve_agent_bridge_pairing(
        agent_id=agent.id,
        user_code="CODE-3",
        current_user=grantee,
        db=object(),
    )

    assert str(captured["user_id"]) == str(grantee.id)
    assert "approval_actor_user_id" not in captured["metadata"]


@pytest.mark.asyncio
async def test_platform_admin_from_selected_company_binds_host_owner(monkeypatch):
    import app.api.local_bridge as local_bridge_api

    tenant_id, employee_id, admin_id, agent = _world()
    platform_admin = SimpleNamespace(id=admin_id, role="platform_admin", tenant_id=tenant_id)
    captured = {}

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_approve(_db, *, user_id, metadata, **_kwargs):
        captured.update(user_id=user_id, metadata=metadata)
        return {"status": "approved"}

    monkeypatch.setattr(local_bridge_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(local_bridge_api.bridge_service, "approve_pairing_session", fake_approve)

    await local_bridge_api.approve_agent_bridge_pairing(
        agent_id=agent.id,
        user_code="CODE-4",
        current_user=platform_admin,
        db=object(),
    )

    assert str(captured["user_id"]) == str(employee_id)
    assert captured["metadata"]["approval_actor_role"] == "platform_admin"
