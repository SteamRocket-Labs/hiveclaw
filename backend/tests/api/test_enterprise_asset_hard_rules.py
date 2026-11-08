from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_agent_deactivate_requires_admin_even_for_creator(monkeypatch):
    import app.api.agents as agents_api

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id, is_active=True)
    agent = SimpleNamespace(id=agent_id, creator_id=user_id, owner_user_id=user_id,
        tenant_id=tenant_id, name="Creator Owned Agent", agent_class="employee",
        home_business_team_id=None, deactivated_at=None, status="active")

    async def fake_check_agent_history_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_check_agent_history_access)

    from app.services import agent_retirement

    monkeypatch.setattr("app.services.runtime_user_identity.load_runtime_user", AsyncMock(return_value=user))
    withdrawal = AsyncMock()
    monkeypatch.setattr(agent_retirement, "external_work", withdrawal)
    # The locked employee exists, but no private D1 creation draft does.
    db = SimpleNamespace(scalar=AsyncMock(side_effect=[agent, None]), rollback=AsyncMock(),
        refresh=AsyncMock(), commit=AsyncMock(), execute=AsyncMock(), add=Mock())

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.deactivate_agent(agent_id=agent_id, current_user=user, db=db)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "agent_retirement_administrator_required"
    assert db.scalar.await_count == 2
    withdrawal.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.execute.assert_not_awaited()
    db.add.assert_not_called()
    assert agent.deactivated_at is None and agent.status == "active"
