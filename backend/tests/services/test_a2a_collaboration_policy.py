from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _agent(
    *,
    agent_id=None,
    tenant_id=None,
    owner_user_id=None,
    creator_id=None,
    name="agent",
    status="running",
    agent_class="internal_tenant",
):
    creator = creator_id or uuid4()
    return SimpleNamespace(
        id=agent_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        owner_user_id=owner_user_id,
        creator_id=creator,
        name=name,
        role_description="",
        status=status,
        agent_type="native",
        agent_class=agent_class,
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
async def test_system_hr_target_is_never_a2a_collaborator(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    tenant_id = uuid4()
    owner_id = uuid4()
    source = _agent(tenant_id=tenant_id, owner_user_id=owner_id, name="source")
    target = _agent(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        name="__system_hr__",
        agent_class="internal_system",
    )

    async def fake_is_public(*_args, **_kwargs):
        raise AssertionError("system HR target must be denied before public A2A lookup")

    async def fake_find_edge(*_args, **_kwargs):
        raise AssertionError("system HR target must be denied before group lookup")

    monkeypatch.setattr(mod, "_is_public_agent", fake_is_public)
    monkeypatch.setattr(mod, "_find_active_collaboration_edge", fake_find_edge)

    result = await mod.resolve_a2a_collaboration_policy(None, source, target, action="delegate")

    assert result.allowed is False
    assert result.reason == "system_hr_a2a_disabled"
    assert "System HR" in result.message


@pytest.mark.asyncio
async def test_system_hr_source_cannot_initiate_a2a(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    tenant_id = uuid4()
    owner_id = uuid4()
    source = _agent(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        name="__system_hr__",
        agent_class="internal_system",
    )
    target = _agent(tenant_id=tenant_id, owner_user_id=owner_id, name="target")

    async def fake_is_public(*_args, **_kwargs):
        raise AssertionError("system HR source must be denied before public A2A lookup")

    async def fake_find_edge(*_args, **_kwargs):
        raise AssertionError("system HR source must be denied before group lookup")

    monkeypatch.setattr(mod, "_is_public_agent", fake_is_public)
    monkeypatch.setattr(mod, "_find_active_collaboration_edge", fake_find_edge)

    result = await mod.resolve_a2a_collaboration_policy(None, source, target, action="message")

    assert result.allowed is False
    assert result.reason == "system_hr_a2a_disabled"


@pytest.mark.asyncio
async def test_non_active_target_status_denies_before_any_collaboration_route(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    tenant_id = uuid4()
    owner_id = uuid4()
    source = _agent(tenant_id=tenant_id, owner_user_id=owner_id, name="source")
    target = _agent(tenant_id=tenant_id, owner_user_id=owner_id, name="target", status="draft")

    async def fake_is_public(*_args, **_kwargs):
        raise AssertionError("inactive targets must be denied before public A2A lookup")

    async def fake_find_edge(*_args, **_kwargs):
        raise AssertionError("inactive targets must be denied before group lookup")

    monkeypatch.setattr(mod, "_is_public_agent", fake_is_public)
    monkeypatch.setattr(mod, "_find_active_collaboration_edge", fake_find_edge)

    result = await mod.resolve_a2a_collaboration_policy(None, source, target, action="delegate")

    assert result.allowed is False
    assert result.reason == "target_unavailable"
    assert "draft" in result.message


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
async def test_cross_owner_public_agent_allows_without_group(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    tenant_id = uuid4()
    source = _agent(tenant_id=tenant_id, owner_user_id=uuid4(), name="source")
    target = _agent(tenant_id=tenant_id, owner_user_id=uuid4(), name="public target")

    async def fake_is_public(*_args, **_kwargs):
        return True

    async def fake_find_edge(*_args, **_kwargs):
        raise AssertionError("public A2A must not require a collaboration group lookup")

    monkeypatch.setattr(mod, "_is_public_agent", fake_is_public)
    monkeypatch.setattr(mod, "_find_active_collaboration_edge", fake_find_edge)

    result = await mod.resolve_a2a_collaboration_policy(None, source, target, action="delegate")

    assert result.allowed is True
    assert result.reason == "public_agent"
    assert result.approval_required is False


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


def test_a2a_collaborators_prompt_keeps_explicit_empty_state():
    from app.runtime.prompt_sections.a2a_collaborators import build_a2a_collaborators_section

    section = build_a2a_collaborators_section(
        {"same_owner_agents": [], "public_agents": [], "collaboration_groups": []}
    )

    assert "## A2A Collaborators" in section
    assert "No governed A2A collaborators" in section


def test_a2a_collaborators_prompt_preserves_every_governed_agent():
    from app.runtime.prompt_sections.a2a_collaborators import build_a2a_collaborators_section

    agents = [
        {
            "id": str(uuid4()),
            "name": f"agent-{index}",
            "role_description": ("role " * 100) + f"END_ROLE_{index}",
            "status": "active",
        }
        for index in range(30)
    ]

    section = build_a2a_collaborators_section(
        {"same_owner_agents": agents, "public_agents": [], "collaboration_groups": []},
        max_chars=200,
    )

    assert "agent-0" in section
    assert "agent-29" in section
    assert "END_ROLE_29" in section
    assert "truncated" not in section


@pytest.mark.asyncio
async def test_a2a_read_model_includes_public_agents(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    source = _agent(name="source")
    same_owner = _agent(tenant_id=source.tenant_id, owner_user_id=source.owner_user_id, name="same")
    system_hr = _agent(
        tenant_id=source.tenant_id,
        owner_user_id=source.owner_user_id,
        name="__system_hr__",
        agent_class="internal_system",
    )
    public_peer = _agent(tenant_id=source.tenant_id, owner_user_id=uuid4(), name="public")
    public_system_hr = _agent(
        tenant_id=source.tenant_id,
        owner_user_id=uuid4(),
        name="__system_hr__",
        agent_class="internal_system",
    )
    group_hr_member = SimpleNamespace(
        agent_id=system_hr.id,
        name="__system_hr__",
        role_description="",
        status="active",
        role="member",
        owner_user_id=source.owner_user_id,
        agent_class="internal_system",
    )
    group_public_member = SimpleNamespace(
        agent_id=public_peer.id,
        name="public",
        role_description="",
        status="active",
        role="member",
        owner_user_id=public_peer.owner_user_id,
        agent_class="internal_tenant",
    )

    class FakeDb:
        async def get(self, model, agent_id):  # noqa: ARG002
            return source

    async def fake_same_owner(*_args, **_kwargs):
        return [same_owner, system_hr]

    async def fake_public(*_args, **_kwargs):
        return [public_peer, public_system_hr]

    async def fake_groups(*_args, **_kwargs):
        return [
            SimpleNamespace(
                group_id=uuid4(),
                group_name="group",
                purpose="",
                status="active",
                members=[group_hr_member, group_public_member],
            )
        ]

    monkeypatch.setattr(mod, "list_same_owner_agents", fake_same_owner)
    monkeypatch.setattr(mod, "list_public_agents", fake_public)
    monkeypatch.setattr(mod, "list_active_collaboration_groups_for_agent", fake_groups)

    read_model = await mod.build_a2a_collaboration_read_model(FakeDb(), source.id)

    assert [agent["name"] for agent in read_model["same_owner_agents"]] == ["same"]
    assert [agent["name"] for agent in read_model["public_agents"]] == ["public"]
    assert read_model["collaboration_groups"][0]["members"] == [
        {
            "agent_id": str(group_public_member.agent_id),
            "id": str(group_public_member.agent_id),
            "name": "public",
            "role_description": "",
            "status": "active",
            "role": "member",
            "owner_user_id": str(group_public_member.owner_user_id),
        }
    ]


@pytest.mark.asyncio
async def test_system_hr_source_read_model_is_empty(monkeypatch):
    from app.services import a2a_collaboration_policy as mod

    source = _agent(name="__system_hr__", agent_class="internal_system")

    class FakeDb:
        async def get(self, model, agent_id):  # noqa: ARG002
            return source

    async def fake_same_owner(*_args, **_kwargs):
        raise AssertionError("system HR source must not load same-owner A2A candidates")

    async def fake_public(*_args, **_kwargs):
        raise AssertionError("system HR source must not load public A2A candidates")

    async def fake_groups(*_args, **_kwargs):
        raise AssertionError("system HR source must not load A2A groups")

    monkeypatch.setattr(mod, "list_same_owner_agents", fake_same_owner)
    monkeypatch.setattr(mod, "list_public_agents", fake_public)
    monkeypatch.setattr(mod, "list_active_collaboration_groups_for_agent", fake_groups)

    assert await mod.build_a2a_collaboration_read_model(FakeDb(), source.id) == {
        "same_owner_agents": [],
        "public_agents": [],
        "collaboration_groups": [],
    }
