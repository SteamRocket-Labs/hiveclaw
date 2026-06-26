from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


class _FakeDB:
    async def commit(self):
        return None

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_agent_delete_requires_admin_even_for_creator(monkeypatch):
    import app.api.agents as agents_api

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    agent = SimpleNamespace(id=agent_id, creator_id=user_id, tenant_id=tenant_id, name="Creator Owned Agent")
    soft_delete_calls: list[uuid.UUID] = []

    async def fake_check_agent_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    async def fake_soft_delete_agent(_db, target_agent, *, actor_id, reason):
        soft_delete_calls.append(target_agent.id)

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "soft_delete_agent", fake_soft_delete_agent)

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.delete_agent(agent_id=agent_id, current_user=user, db=_FakeDB())

    assert exc_info.value.status_code == 403
    assert "admin" in str(exc_info.value.detail).lower()
    assert soft_delete_calls == []


@pytest.mark.asyncio
async def test_desktop_sub_agent_delete_requires_admin_even_for_owner(monkeypatch):
    import app.api.desktop_agents as desktop_agents_api

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    agent = SimpleNamespace(id=agent_id, creator_id=user_id, tenant_id=tenant_id, name="Owned Sub Agent")
    soft_delete_calls: list[uuid.UUID] = []

    async def fake_get_owned_sub_agent(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent

    async def fake_soft_delete_agent(_db, target_agent, *, actor_id, reason):
        soft_delete_calls.append(target_agent.id)

    async def fake_bump_sync_version(_db, _tenant_id):
        raise AssertionError("sync version should not bump for denied delete")

    monkeypatch.setattr(desktop_agents_api, "_get_owned_sub_agent", fake_get_owned_sub_agent)
    monkeypatch.setattr(desktop_agents_api, "soft_delete_agent", fake_soft_delete_agent)
    monkeypatch.setattr(desktop_agents_api, "bump_sync_version", fake_bump_sync_version)

    with pytest.raises(HTTPException) as exc_info:
        await desktop_agents_api.delete_sub_agent(agent_id=agent_id, current_user=user, db=_FakeDB())

    assert exc_info.value.status_code == 403
    assert "admin" in str(exc_info.value.detail).lower()
    assert soft_delete_calls == []
