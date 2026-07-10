from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.flushed = False
        self.added = []

    async def execute(self, _stmt):
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_list_handover_candidates_returns_active_same_tenant_users(monkeypatch):
    import app.api.advanced as advanced_api

    tenant_id = uuid4()
    agent_id = uuid4()
    creator_id = uuid4()
    candidate_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id, owner_user_id=creator_id, tenant_id=tenant_id)

    async def fake_require_manage(db, current_user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent

    monkeypatch.setattr(advanced_api, "require_agent_manage_access", fake_require_manage)

    db = _FakeDB(
        [
            _ListResult(
                [
                    SimpleNamespace(
                        id=candidate_id,
                        display_name="Alice",
                        email="alice@example.com",
                        role="member",
                        is_active=True,
                    ),
                ]
            ),
        ]
    )

    result = await advanced_api.list_handover_candidates(
        agent_id=agent_id,
        current_user=SimpleNamespace(id=creator_id, tenant_id=tenant_id),
        db=db,
    )

    assert result == [
        {
            "id": str(candidate_id),
            "display_name": "Alice",
            "email": "alice@example.com",
            "role": "member",
        }
    ]


@pytest.mark.asyncio
async def test_handover_rejects_target_user_from_other_tenant(monkeypatch):
    import app.api.advanced as advanced_api

    tenant_id = uuid4()
    creator_id = uuid4()
    agent_id = uuid4()
    target_user_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=creator_id,
        owner_user_id=creator_id,
        tenant_id=tenant_id,
        name="Ops Bot",
    )
    foreign_user = SimpleNamespace(
        id=target_user_id,
        tenant_id=uuid4(),
        is_active=True,
        display_name="Bob",
    )

    async def fake_require_manage(db, current_user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent

    monkeypatch.setattr(advanced_api, "require_agent_manage_access", fake_require_manage)

    db = _FakeDB([_ScalarResult(foreign_user)])

    with pytest.raises(HTTPException) as exc:
        await advanced_api.handover_agent(
            agent_id=agent_id,
            data=SimpleNamespace(new_owner_id=target_user_id),
            current_user=SimpleNamespace(id=creator_id, tenant_id=tenant_id),
            db=db,
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_handover_changes_current_owner_without_rewriting_creator_provenance(monkeypatch):
    import app.api.advanced as advanced_api

    tenant_id = uuid4()
    creator_id = uuid4()
    agent_id = uuid4()
    target_user_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=creator_id,
        owner_user_id=creator_id,
        tenant_id=tenant_id,
        name="Ops Bot",
    )
    target_user = SimpleNamespace(
        id=target_user_id,
        tenant_id=tenant_id,
        is_active=True,
        display_name="Bob",
    )

    async def fake_require_manage(db, current_user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent

    monkeypatch.setattr(advanced_api, "require_agent_manage_access", fake_require_manage)

    db = _FakeDB([_ScalarResult(target_user)])

    result = await advanced_api.handover_agent(
        agent_id=agent_id,
        data=SimpleNamespace(new_owner_id=target_user_id),
        current_user=SimpleNamespace(id=creator_id, tenant_id=tenant_id),
        db=db,
    )

    assert result["status"] == "transferred"
    assert result["new_owner"] == "Bob"
    assert agent.owner_user_id == target_user_id
    assert agent.creator_id == creator_id
    assert db.flushed is True
