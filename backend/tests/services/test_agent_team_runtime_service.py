from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _DB:
    def __init__(self) -> None:
        self.added = []
        self.flushes = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


@pytest.mark.asyncio
async def test_create_agent_team_runtime_persists_member_sessions_and_parent_events(monkeypatch):
    from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
    from app.models.chat_session import ChatSession
    from app.services.agent_team_runtime_service import TeamMemberCreateSpec, create_agent_team_runtime

    db = _DB()
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    parent_session = SimpleNamespace(id=uuid4(), root_session_id=None)
    emitted = []
    parent_events = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event.value, kwargs))

    async def fake_append_session_event(**kwargs):
        parent_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr("app.services.agent_team_runtime_service.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.services.agent_team_runtime_service.append_session_event", fake_append_session_event)

    payload = await create_agent_team_runtime(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        name="Parity Review",
        members=[TeamMemberCreateSpec(name="critic", role="Review CCPlus gaps")],
        source="unit_test",
        command="team_create",
    )

    assert payload["requires_api_persist"] is False
    assert payload["status"] == "active"
    assert payload["members"][0]["member_name"] == "critic"
    assert {type(item).__name__ for item in db.added} >= {
        "AgentTeam",
        "AgentTeamMember",
        "AgentTeamEvent",
        "ChatSession",
    }
    team = next(item for item in db.added if isinstance(item, AgentTeam))
    member = next(item for item in db.added if isinstance(item, AgentTeamMember))
    member_session = next(item for item in db.added if isinstance(item, ChatSession))
    event = next(item for item in db.added if isinstance(item, AgentTeamEvent))
    assert team.parent_session_id == parent_session.id
    assert member.team_id == team.id
    assert member.chat_session_id == member_session.id
    assert event.event_type == "team_created"
    assert emitted[0][0] == "team_created"
    assert parent_events[0]["event_type"] == "team_member"
    assert parent_events[0]["metadata"]["team_id"] == str(team.id)
    assert parent_events[0]["metadata"]["child_session_id"] == str(member_session.id)


@pytest.mark.asyncio
async def test_message_agent_team_members_runtime_broadcasts_to_member_sessions(monkeypatch):
    from app.models.agent_team import AgentTeamEvent
    from app.services.agent_team_runtime_service import (
        TeamMemberCreateSpec,
        create_agent_team_runtime_result,
        message_agent_team_members_runtime,
    )

    db = _DB()
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    parent_session = SimpleNamespace(id=uuid4(), root_session_id=None)
    continued = []

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(event_id=uuid4())

    async def fake_continue_agent_session_from_mailbox(**kwargs):
        continued.append(kwargs)
        return {"ok": True, "status": "queued", "consumer": "mid_run_message_drain", "run_id": str(uuid4())}

    monkeypatch.setattr("app.services.agent_team_runtime_service.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.services.agent_team_runtime_service.append_session_event", fake_append_session_event)
    monkeypatch.setattr(
        "app.services.agent_team_runtime_service.continue_agent_session_from_mailbox",
        fake_continue_agent_session_from_mailbox,
    )

    created = await create_agent_team_runtime_result(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        name="Parity Review",
        members=[
            TeamMemberCreateSpec(name="researcher", role="Research"),
            TeamMemberCreateSpec(name="critic", role="Review"),
        ],
        source="unit_test",
    )
    payload = await message_agent_team_members_runtime(
        db=db,
        agent=agent,
        user=user,
        team=created.team,
        members=created.members,
        member_sessions=created.member_sessions,
        message="check your slice",
        source="unit_test",
    )

    assert payload["ok"] is True
    assert payload["message_count"] == 2
    assert [call["session"].id for call in continued] == [session.id for session in created.member_sessions]
    queued_events = [
        item for item in db.added if isinstance(item, AgentTeamEvent) and item.event_type == "member_message_queued"
    ]
    assert len(queued_events) == 2
