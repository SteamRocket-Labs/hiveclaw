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

    async def fake_require_owner_or_admin(db, current_user, requested_agent_id, **_kwargs):
        assert requested_agent_id == agent_id
        return agent

    monkeypatch.setattr(advanced_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)

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
    import app.services.agent_ownership_service as ownership_service

    tenant_id = uuid4()
    creator_id = uuid4()
    agent_id = uuid4()
    target_user_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=creator_id,
        owner_user_id=creator_id,
        sponsor_user_id=creator_id,
        tenant_id=tenant_id,
        name="Ops Bot",
    )
    foreign_user = SimpleNamespace(
        id=target_user_id,
        tenant_id=uuid4(),
        is_active=True,
        display_name="Bob",
    )

    async def fake_require_owner_or_admin(db, current_user, requested_agent_id, **_kwargs):
        assert requested_agent_id == agent_id
        return agent

    monkeypatch.setattr(ownership_service, "require_agent_owner_or_admin", fake_require_owner_or_admin)

    db = _FakeDB([_ScalarResult(foreign_user)])

    with pytest.raises(HTTPException) as exc:
        await advanced_api.handover_agent(
            agent_id=agent_id,
            data=advanced_api.HandoverRequest(new_owner_id=target_user_id, reason="Manual transfer"),
            current_user=SimpleNamespace(id=creator_id, tenant_id=tenant_id),
            db=db,
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_handover_changes_current_owner_without_rewriting_creator_provenance(monkeypatch):
    import app.api.advanced as advanced_api
    import app.services.agent_ownership_service as ownership_service

    tenant_id = uuid4()
    creator_id = uuid4()
    agent_id = uuid4()
    target_user_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=creator_id,
        owner_user_id=creator_id,
        sponsor_user_id=creator_id,
        tenant_id=tenant_id,
        name="Ops Bot",
    )
    target_user = SimpleNamespace(
        id=target_user_id,
        tenant_id=tenant_id,
        is_active=True,
        display_name="Bob",
    )

    async def fake_require_owner_or_admin(db, current_user, requested_agent_id, **_kwargs):
        assert requested_agent_id == agent_id
        return agent

    monkeypatch.setattr(ownership_service, "require_agent_owner_or_admin", fake_require_owner_or_admin)

    async def fake_register_agent_asset(*_args, **_kwargs):
        return None

    async def fake_rebind(*_args, **_kwargs):
        return []

    monkeypatch.setattr(ownership_service, "register_agent_asset", fake_register_agent_asset)
    monkeypatch.setattr(ownership_service, "_rebind_active_collaboration_memberships", fake_rebind)

    db = _FakeDB([_ScalarResult(target_user)])

    result = await advanced_api.handover_agent(
        agent_id=agent_id,
        data=advanced_api.HandoverRequest(
            new_owner_id=target_user_id,
            expected_owner_id=creator_id,
            reason="Manual transfer",
            request_id="handover-1",
        ),
        current_user=SimpleNamespace(id=creator_id, tenant_id=tenant_id),
        db=db,
    )

    assert result["status"] == "transferred"
    assert result["new_owner"] == "Bob"
    assert agent.owner_user_id == target_user_id
    assert agent.creator_id == creator_id
    assert agent.sponsor_user_id == creator_id
    assert db.flushed is True
