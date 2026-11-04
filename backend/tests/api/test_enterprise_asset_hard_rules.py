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
async def test_agent_deactivate_requires_admin_even_for_creator(monkeypatch):
    import app.api.agents as agents_api

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    agent = SimpleNamespace(id=agent_id, creator_id=user_id, tenant_id=tenant_id, name="Creator Owned Agent")

    async def fake_check_agent_history_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_check_agent_history_access)

    from app.services import agent_retirement

    async def fail_prepare(*_args, **_kwargs):
        raise AssertionError("prepare must not run for a non-admin deactivation")

    monkeypatch.setattr(agent_retirement, "prepare", fail_prepare)

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.deactivate_agent(agent_id=agent_id, current_user=user, db=_FakeDB())

    assert exc_info.value.status_code == 403
    assert "admin" in str(exc_info.value.detail).lower()
