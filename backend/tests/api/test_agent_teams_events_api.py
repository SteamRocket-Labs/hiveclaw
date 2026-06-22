from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        value = self._value if isinstance(self._value, list) else ([] if self._value is None else [self._value])
        return SimpleNamespace(all=lambda: value)


class _DB:
    def __init__(self):
        self.added = []
        self.flushes = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def execute(self, _stmt):
        return _ScalarResult([])


@pytest.mark.asyncio
async def test_team_event_permission_request_persists_and_emits_hook(monkeypatch):
    import app.api.agent_teams as teams_api

    agent_id = uuid4()
    team_id = uuid4()
    parent_session_id = uuid4()
    sender_id = uuid4()
    receiver_id = uuid4()
    tenant_id = uuid4()
    team = AgentTeam(
        id=team_id, tenant_id=tenant_id, lead_agent_id=agent_id, parent_session_id=parent_session_id, name="T"
    )
    members = [
        AgentTeamMember(id=sender_id, team_id=team_id, member_name="sender", chat_session_id=uuid4()),
        AgentTeamMember(id=receiver_id, team_id=team_id, member_name="receiver", chat_session_id=uuid4()),
    ]
    emitted = []
    db = _DB()

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event.value, kwargs))

    async def fake_load_team_or_404(*_args, **_kwargs):
        return team

    async def fake_load_team_members(*_args, **_kwargs):
        return members

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)
    monkeypatch.setattr(teams_api, "_load_team_or_404", fake_load_team_or_404)
    monkeypatch.setattr(teams_api, "_load_team_members", fake_load_team_members)
    monkeypatch.setattr(teams_api, "emit_hook", fake_emit_hook)

    result = await teams_api.create_agent_team_event(
        agent_id=agent_id,
        team_id=team_id,
        body=teams_api.CreateAgentTeamEventIn(
            sender_member_id=sender_id,
            receiver_member_id=receiver_id,
            event_type="permission_request",
            payload={"tool": "send_email"},
        ),
        current_user=SimpleNamespace(id=uuid4(), role="member"),
        db=db,
    )

    assert isinstance(db.added[0], AgentTeamEvent)
    assert db.added[0].payload_json == {"tool": "send_email"}
    assert result["event_type"] == "permission_request"
    assert emitted[0][0] == "permission_request"
    assert emitted[0][1]["metadata"]["team_id"] == str(team_id)


@pytest.mark.asyncio
async def test_team_event_rejects_member_from_other_team(monkeypatch):
    import app.api.agent_teams as teams_api
    from fastapi import HTTPException

    agent_id = uuid4()
    team_id = uuid4()
    team = AgentTeam(id=team_id, tenant_id=uuid4(), lead_agent_id=agent_id, parent_session_id=uuid4(), name="T")
    member = AgentTeamMember(id=uuid4(), team_id=uuid4(), member_name="other", chat_session_id=uuid4())

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=team.tenant_id), "use"

    async def fake_load_team_or_404(*_args, **_kwargs):
        return team

    async def fake_load_team_members(*_args, **_kwargs):
        return [member]

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)
    monkeypatch.setattr(teams_api, "_load_team_or_404", fake_load_team_or_404)
    monkeypatch.setattr(teams_api, "_load_team_members", fake_load_team_members)

    with pytest.raises(HTTPException) as exc:
        await teams_api.create_agent_team_event(
            agent_id=agent_id,
            team_id=team_id,
            body=teams_api.CreateAgentTeamEventIn(
                sender_member_id=member.id,
                event_type="idle",
                payload={},
            ),
            current_user=SimpleNamespace(id=uuid4(), role="member"),
            db=_DB(),
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_list_team_events_returns_mailbox_stream(monkeypatch):
    import app.api.agent_teams as teams_api

    agent_id = uuid4()
    team_id = uuid4()
    event = AgentTeamEvent(id=uuid4(), team_id=team_id, event_type="message", payload_json={"text": "hello"})
    team = AgentTeam(id=team_id, tenant_id=uuid4(), lead_agent_id=agent_id, parent_session_id=uuid4(), name="T")

    class DB(_DB):
        async def execute(self, _stmt):
            return _ScalarResult([event])

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id), "use"

    async def fake_load_team_or_404(*_args, **_kwargs):
        return team

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)
    monkeypatch.setattr(teams_api, "_load_team_or_404", fake_load_team_or_404)

    result = await teams_api.list_agent_team_events(
        agent_id=agent_id,
        team_id=team_id,
        current_user=SimpleNamespace(id=uuid4(), role="member"),
        db=DB(),
    )

    assert result[0]["event_type"] == "message"
    assert result[0]["payload"] == {"text": "hello"}
