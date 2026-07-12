from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

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


@pytest.fixture(autouse=True)
def _allow_session_owner_authority(monkeypatch):
    import app.api.agent_teams as teams_api

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(authority_source="session_owner")

    monkeypatch.setattr(teams_api, "_authorize_team_action", allow)


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


@pytest.mark.asyncio
async def test_start_team_member_run_starts_runtime_and_records_event(monkeypatch):
    import app.api.agent_teams as teams_api

    agent_id = uuid4()
    team_id = uuid4()
    member_id = uuid4()
    member_session_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="Lead")
    team = AgentTeam(id=team_id, tenant_id=tenant_id, lead_agent_id=agent_id, parent_session_id=uuid4(), name="T")
    member = AgentTeamMember(id=member_id, team_id=team_id, member_name="critic", chat_session_id=member_session_id)
    session = SimpleNamespace(
        id=member_session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user.id,
        session_kind="team_member",
        runtime_source="team_member",
        transcript_metadata_json={},
    )
    db = _DB()
    captured: dict = {}

    async def fake_access(*_args, **_kwargs):
        return agent, "manage"

    async def fake_load_team_or_404(*_args, **_kwargs):
        return team

    async def fake_load_member_or_404(*_args, **_kwargs):
        return member

    async def fake_load_member_session_or_404(*_args, **_kwargs):
        return session

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return {"run_id": "00112233445566778899aabbccddeeff", "status": "running"}

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)
    monkeypatch.setattr(teams_api, "_load_team_or_404", fake_load_team_or_404)
    monkeypatch.setattr(teams_api, "_load_team_member_or_404", fake_load_member_or_404, raising=False)
    monkeypatch.setattr(teams_api, "_load_member_session_or_404", fake_load_member_session_or_404, raising=False)
    monkeypatch.setattr(teams_api, "start_web_chat_run", fake_start, raising=False)

    result = await teams_api.start_agent_team_member_run(
        agent_id=agent_id,
        team_id=team_id,
        member_id=member_id,
        body=teams_api.StartAgentTeamMemberRunIn(content="review the hook implementation"),
        current_user=user,
        db=db,
    )

    assert result["status"] == "running"
    assert result["runtime_task_type"] == "team_member"
    assert member.status == "running"
    assert str(member.runtime_task_id) == "00112233-4455-6677-8899-aabbccddeeff"
    assert captured["runtime_task_type"] == "team_member"
    assert captured["extra_metadata"]["team_id"] == str(team_id)
    assert captured["extra_metadata"]["member_id"] == str(member_id)
    assert any(isinstance(item, AgentTeamEvent) and item.event_type == "member_run_started" for item in db.added)


@pytest.mark.asyncio
async def test_message_team_member_uses_mailbox_continuation_consumer(monkeypatch):
    import app.api.agent_teams as teams_api

    agent_id = uuid4()
    team_id = uuid4()
    member_id = uuid4()
    member_session_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="Lead")
    team = AgentTeam(id=team_id, tenant_id=tenant_id, lead_agent_id=agent_id, parent_session_id=uuid4(), name="T")
    member = AgentTeamMember(id=member_id, team_id=team_id, member_name="critic", chat_session_id=member_session_id)
    session = SimpleNamespace(
        id=member_session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user.id,
        session_kind="team_member",
        runtime_source="team_member",
        transcript_metadata_json={"session_state": "open"},
    )
    db = _DB()
    captured: dict = {}

    async def fake_access(*_args, **_kwargs):
        return agent, "manage"

    async def fake_load_team_or_404(*_args, **_kwargs):
        return team

    async def fake_load_member_or_404(*_args, **_kwargs):
        return member

    async def fake_load_member_session_or_404(*_args, **_kwargs):
        return session

    async def fake_continue(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "status": "queued", "run_id": "run-1", "consumer": "mid_run_message_drain"}

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)
    monkeypatch.setattr(teams_api, "_load_team_or_404", fake_load_team_or_404)
    monkeypatch.setattr(teams_api, "_load_team_member_or_404", fake_load_member_or_404, raising=False)
    monkeypatch.setattr(teams_api, "_load_member_session_or_404", fake_load_member_session_or_404, raising=False)
    monkeypatch.setattr(
        "app.services.agent_team_runtime_service.continue_agent_session_from_mailbox",
        fake_continue,
        raising=False,
    )

    result = await teams_api.message_agent_team_member(
        agent_id=agent_id,
        team_id=team_id,
        member_id=member_id,
        body=teams_api.MessageAgentTeamMemberIn(message="tighten the review"),
        current_user=user,
        db=db,
    )

    assert result["status"] == "queued"
    assert result["consumer"] == "mid_run_message_drain"
    assert captured["runtime_task_type"] == "team_member"
    assert captured["message"] == "tighten the review"
    assert member.status == "running"
    assert any(isinstance(item, AgentTeamEvent) and item.event_type == "member_message_queued" for item in db.added)


@pytest.mark.asyncio
async def test_close_team_enqueues_lead_synthesis_without_platform_authored_assistant(monkeypatch):
    import app.api.agent_teams as teams_api

    agent_id = uuid4()
    team_id = uuid4()
    tenant_id = uuid4()
    parent_session_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member")
    team = AgentTeam(
        id=team_id, tenant_id=tenant_id, lead_agent_id=agent_id, parent_session_id=parent_session_id, name="T"
    )
    members = [
        AgentTeamMember(
            id=uuid4(),
            team_id=team_id,
            member_name="critic",
            chat_session_id=uuid4(),
            metadata_json={
                "summary": "审查了 A2A gate",
                "artifacts": ["workspace/a2a.md"],
                "work_ledger_deltas": [{"id": "todo-1", "status": "done"}],
                "t0_refs": ["session-a#event-1"],
            },
        )
    ]
    db = _DB()
    appended: list[dict] = []
    enqueued = []
    load_requests = []

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_load_team_or_404(*_args, **_kwargs):
        load_requests.append(_kwargs)
        return team

    async def fake_load_team_members(*_args, **_kwargs):
        return members

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=1)

    async def fake_enqueue(actual_db, notification):
        enqueued.append((actual_db, notification))
        return uuid4()

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)
    monkeypatch.setattr(teams_api, "_load_team_or_404", fake_load_team_or_404)
    monkeypatch.setattr(teams_api, "_load_team_members", fake_load_team_members)
    monkeypatch.setattr(teams_api, "emit_hook", fake_emit_hook)
    monkeypatch.setattr(teams_api, "append_session_event", fake_append_session_event, raising=False)
    monkeypatch.setattr(teams_api, "enqueue_completion_notification", fake_enqueue, raising=False)

    result = await teams_api.close_agent_team(
        agent_id=agent_id,
        team_id=team_id,
        current_user=user,
        db=db,
    )

    assert result["status"] == "closing"
    assert "consolidation_plan" in result
    assert result["close_delivery"]["status"] == "pending_lead_synthesis"
    assert team.status == "closing"
    assert load_requests[0]["for_update"] is True
    assert members[0].status == "idle"
    assert appended == []
    assert len(enqueued) == 1
    actual_db, notification = enqueued[0]
    assert actual_db is db
    assert notification.source_kind == "agent_team"
    assert notification.task_type == "agent_team_close"
    assert notification.parent_session_id == parent_session_id
    assert notification.parent_agent_id == agent_id
    assert notification.parent_user_id == user.id
    assert notification.metadata["agent_team_close_id"] == str(team_id)
    assert "审查了 A2A gate" in notification.metadata["model_context"]
    assert "session-a#event-1" in notification.metadata["model_context"]
    assert any(isinstance(item, AgentTeamEvent) and item.event_type == "team_close_requested" for item in db.added)


def test_team_workbench_counts_only_actively_running_members():
    import app.api.agent_teams as teams_api

    team = AgentTeam(
        id=uuid4(),
        tenant_id=uuid4(),
        lead_agent_id=uuid4(),
        parent_session_id=uuid4(),
        name="T",
        metadata_json={"close_synthesis_status": "failed", "close_failure": "Provider timeout"},
    )
    members = [
        AgentTeamMember(
            id=uuid4(),
            team_id=team.id,
            member_name="done",
            chat_session_id=uuid4(),
            status="idle",
            metadata_json={"last_turn_status": "completed"},
        ),
        AgentTeamMember(
            id=uuid4(),
            team_id=team.id,
            member_name="running",
            chat_session_id=uuid4(),
            status="running",
        ),
        AgentTeamMember(
            id=uuid4(),
            team_id=team.id,
            member_name="pending",
            chat_session_id=uuid4(),
            status="pending",
        ),
    ]

    result = teams_api._team_workbench_payload(agent_id=team.lead_agent_id, team=team, members=members, events=[])

    assert result["summary"]["active_member_count"] == 2
    assert result["team"]["close_status"] == "failed"
    assert result["team"]["close_failure"] == "Provider timeout"
    assert result["members"][0]["last_turn_status"] == "completed"
