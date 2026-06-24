from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _agent(*, agent_id=None, tenant_id=None, owner_user_id=None, creator_id=None, name="agent"):
    creator = creator_id or uuid4()
    return SimpleNamespace(
        id=agent_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        owner_user_id=owner_user_id,
        creator_id=creator,
        name=name,
    )


def test_owner_resolver_prefers_owner_user_id_and_falls_back_to_creator_id():
    from app.services.a2a_collaboration_policy import resolve_agent_owner_id

    owner_id = uuid4()
    creator_id = uuid4()
    assert resolve_agent_owner_id(_agent(owner_user_id=owner_id, creator_id=creator_id)) == owner_id
    assert resolve_agent_owner_id(_agent(owner_user_id=None, creator_id=creator_id)) == creator_id


@pytest.mark.asyncio
async def test_same_owner_policy_allows_without_explicit_group(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    tenant_id = uuid4()
    owner_id = uuid4()
    source = _agent(tenant_id=tenant_id, owner_user_id=owner_id, name="source")
    target = _agent(tenant_id=tenant_id, owner_user_id=owner_id, name="target")

    async def fake_find_edge(*_args, **_kwargs):
        raise AssertionError("same-owner policy must not require a group lookup")

    monkeypatch.setattr(mod, "_find_active_collaboration_edge", fake_find_edge)

    result = await mod.resolve_a2a_collaboration_policy(None, source, target, action="message")

    assert result.allowed is True
    assert result.reason == "same_owner"
    assert result.approval_required is False


@pytest.mark.asyncio
async def test_cross_owner_without_active_group_fails_closed(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    tenant_id = uuid4()
    source = _agent(tenant_id=tenant_id, owner_user_id=uuid4(), name="source")
    target = _agent(tenant_id=tenant_id, owner_user_id=uuid4(), name="target")

    async def fake_find_edge(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mod, "_find_active_collaboration_edge", fake_find_edge)

    result = await mod.resolve_a2a_collaboration_policy(None, source, target, action="delegate")

    assert result.allowed is False
    assert result.reason == "no_group"
    assert result.approval_required is True
    assert "A2A Collaboration Group" in result.message


@pytest.mark.asyncio
async def test_cross_owner_active_group_allows(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    tenant_id = uuid4()
    group_id = uuid4()
    source = _agent(tenant_id=tenant_id, owner_user_id=uuid4(), name="source")
    target = _agent(tenant_id=tenant_id, owner_user_id=uuid4(), name="target")

    async def fake_find_edge(*_args, **_kwargs):
        return SimpleNamespace(group_id=group_id, group_name="Launch room", status="active")

    monkeypatch.setattr(mod, "_find_active_collaboration_edge", fake_find_edge)

    result = await mod.resolve_a2a_collaboration_policy(None, source, target, action="delegate")

    assert result.allowed is True
    assert result.reason == "active_group"
    assert result.group_id == group_id
    assert result.group_name == "Launch room"


@pytest.mark.asyncio
async def test_cross_owner_pending_or_revoked_group_denies(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    tenant_id = uuid4()
    source = _agent(tenant_id=tenant_id, owner_user_id=uuid4(), name="source")
    target = _agent(tenant_id=tenant_id, owner_user_id=uuid4(), name="target")

    for status in ("pending_owner_confirmation", "revoked", "rejected"):

        async def fake_find_edge(*_args, status=status, **_kwargs):
            return SimpleNamespace(group_id=uuid4(), group_name="Blocked room", status=status)

        monkeypatch.setattr(mod, "_find_active_collaboration_edge", fake_find_edge)
        result = await mod.resolve_a2a_collaboration_policy(None, source, target, action="delegate")
        assert result.allowed is False
        assert result.reason == status
