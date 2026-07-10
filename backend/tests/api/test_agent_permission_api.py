from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _PermissionUpdateDB:
    def __init__(self) -> None:
        self.executed: list[object] = []
        self.added: list[object] = []
        self.committed = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace()

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_org_admin_can_update_same_tenant_agent_permissions(monkeypatch):
    from app.api import agents as agents_api

    agent_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id)
    current_user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    db = _PermissionUpdateDB()

    async def fake_require_manage(_db, _user, _agent_id):
        return agent

    monkeypatch.setattr(agents_api, "require_agent_manage_access", fake_require_manage)

    result = await agents_api.update_agent_permissions(
        agent_id,
        {"scope_type": "company", "scope_ids": [], "access_level": "manage"},
        current_user=current_user,
        db=db,
    )

    assert result == {"status": "ok"}
    assert db.committed is True
    assert len(db.added) == 1
    assert db.added[0].scope_type == "company"
    assert db.added[0].access_level == "manage"


@pytest.mark.asyncio
async def test_member_non_owner_cannot_update_agent_permissions(monkeypatch):
    from app.api import agents as agents_api

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=agent.tenant_id)
    db = _PermissionUpdateDB()

    async def fake_require_manage(_db, _user, _agent_id):
        raise HTTPException(status_code=403, detail="Manage access required")

    monkeypatch.setattr(agents_api, "require_agent_manage_access", fake_require_manage)

    with pytest.raises(HTTPException) as exc:
        await agents_api.update_agent_permissions(
            agent_id,
            {"scope_type": "company", "scope_ids": [], "access_level": "manage"},
            current_user=current_user,
            db=db,
        )

    assert exc.value.status_code == 403
    assert db.committed is False
