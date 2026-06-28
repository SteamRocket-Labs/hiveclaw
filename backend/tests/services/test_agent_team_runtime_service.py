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


class _ScalarOne:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _CompletionDB(_DB):
    def __init__(self, value) -> None:
        super().__init__()
        self.value = value
        self.executes = 0

    async def execute(self, _stmt):
        self.executes += 1
        return _ScalarOne(self.value)


@pytest.mark.asyncio
async def test_team_create_runtime_persists_container_without_teammate_sessions(monkeypatch):
    from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
    from app.models.chat_session import ChatSession
    from app.services.agent_team_runtime_service import create_agent_team_runtime

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
        members=[],
        source="unit_test",
        command="team_create",
    )

    assert payload["requires_api_persist"] is False
    assert payload["status"] == "active"
    assert payload["members"] == []
    team = next(item for item in db.added if isinstance(item, AgentTeam))
    event = next(item for item in db.added if isinstance(item, AgentTeamEvent))
    assert team.parent_session_id == parent_session.id
    assert not any(isinstance(item, AgentTeamMember) for item in db.added)
    assert not any(isinstance(item, ChatSession) for item in db.added)
    assert event.event_type == "team_created"
    assert event.payload_json["member_count"] == 0
    assert emitted[0][0] == "team_created"
    assert parent_events == []


@pytest.mark.asyncio
async def test_agenttool_teammate_spawn_creates_member_session_and_starts_runtime(monkeypatch):
    from app.models.agent_team import AgentTeamEvent, AgentTeamMember
    from app.models.chat_session import ChatSession
    from app.services.agent_team_runtime_service import (
        TeamMemberCreateSpec,
        create_agent_team_runtime_result,
        spawn_agent_team_member_runtime,
    )

    db = _DB()
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    parent_session = SimpleNamespace(id=uuid4(), root_session_id=None)
    continued = []
    parent_events = []

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    async def fake_append_session_event(**kwargs):
        parent_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    async def fake_continue_agent_session_from_mailbox(**kwargs):
        continued.append(kwargs)
        return {"ok": True, "status": "queued", "consumer": "teammate_spawn", "run_id": str(uuid4())}

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
        members=[],
        source="unit_test",
    )
    payload = await spawn_agent_team_member_runtime(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=created.team,
        spec=TeamMemberCreateSpec(
            name="critic",
            role="Review CCPlus gaps",
            metadata={"agent_type": "critic"},
        ),
        prompt="Review the AgentTool implementation.",
        source="agent_tool_teammate_spawn",
    )

    assert payload["ok"] is True
    assert payload["status"] == "teammate_spawned"
    assert payload["member"]["member_name"] == "critic"
    member = next(item for item in db.added if isinstance(item, AgentTeamMember))
    member_session = next(item for item in db.added if isinstance(item, ChatSession))
    assert member.team_id == created.team.id
    assert member.chat_session_id == member_session.id
    assert member.status == "running"
    assert continued and continued[0]["session"].id == member_session.id
    assert continued[0]["message"] == "Review the AgentTool implementation."
    event_types = [item.event_type for item in db.added if isinstance(item, AgentTeamEvent)]
    assert "member_spawned" in event_types
    assert "member_message_queued" in event_types
    assert parent_events and parent_events[0]["event_type"] == "team_member"


@pytest.mark.asyncio
async def test_team_create_runtime_rejects_inline_member_specs(monkeypatch):
    from app.services.agent_team_runtime_service import (
        TeamMemberCreateSpec,
        create_agent_team_runtime_result,
    )

    db = _DB()
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    parent_session = SimpleNamespace(id=uuid4(), root_session_id=None)

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.agent_team_runtime_service.emit_hook", fake_emit_hook)

    with pytest.raises(ValueError, match="TeamCreate creates the Team container only"):
        await create_agent_team_runtime_result(
            db=db,
            agent=agent,
            user=user,
            parent_session=parent_session,
            name="Parity Review",
            members=[TeamMemberCreateSpec(name="critic", role="Review")],
            source="unit_test",
        )


@pytest.mark.asyncio
async def test_message_agent_team_members_runtime_broadcasts_to_member_sessions(monkeypatch):
    from app.models.agent_team import AgentTeamEvent, AgentTeamMember
    from app.models.chat_session import ChatSession
    from app.services.agent_team_runtime_service import (
        TeamMemberCreateSpec,
        create_agent_team_runtime_result,
        message_agent_team_members_runtime,
        spawn_agent_team_member_runtime,
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
        members=[],
        source="unit_test",
    )
    await spawn_agent_team_member_runtime(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=created.team,
        spec=TeamMemberCreateSpec(name="researcher", role="Research"),
        prompt="Research your slice.",
        source="unit_test",
    )
    await spawn_agent_team_member_runtime(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=created.team,
        spec=TeamMemberCreateSpec(name="critic", role="Review"),
        prompt="Review your slice.",
        source="unit_test",
    )
    spawned_members = [item for item in db.added if isinstance(item, AgentTeamMember)]
    spawned_sessions = [item for item in db.added if isinstance(item, ChatSession)]
    continued.clear()
    queued_before = len(
        [item for item in db.added if isinstance(item, AgentTeamEvent) and item.event_type == "member_message_queued"]
    )
    payload = await message_agent_team_members_runtime(
        db=db,
        agent=agent,
        user=user,
        team=created.team,
        members=spawned_members,
        member_sessions=spawned_sessions,
        message="check your slice",
        source="unit_test",
    )

    assert payload["ok"] is True
    assert payload["message_count"] == 2
    assert [call["session"].id for call in continued] == [session.id for session in spawned_sessions]
    queued_events = [
        item for item in db.added if isinstance(item, AgentTeamEvent) and item.event_type == "member_message_queued"
    ]
    assert len(queued_events) - queued_before == 2


@pytest.mark.asyncio
async def test_team_member_completion_projects_to_member_metadata_and_event():
    from app.models.agent_team import AgentTeamEvent, AgentTeamMember
    from app.services.agent_team_runtime_service import project_agent_team_member_completion

    run_id = uuid4()
    member = AgentTeamMember(
        id=uuid4(),
        team_id=uuid4(),
        member_name="critic",
        chat_session_id=uuid4(),
        runtime_task_id=run_id,
        metadata_json={"existing": "keep"},
    )
    db = _CompletionDB(member)
    task = SimpleNamespace(
        id=run_id,
        task_type="team_member",
        child_session_id=str(member.chat_session_id),
        metadata_json={
            "artifact_paths": ["workspace/review.md"],
            "artifacts": [{"path": "workspace/review.md", "type": "artifact"}],
            "t0_refs": ["session#event-1"],
        },
    )

    payload = await project_agent_team_member_completion(
        db=db,
        task=task,
        status="completed",
        result_summary="review passed",
        metadata_json=task.metadata_json,
    )

    assert payload is not None
    assert member.status == "completed"
    assert member.metadata_json["existing"] == "keep"
    assert member.metadata_json["summary"] == "review passed"
    assert member.metadata_json["artifact_paths"] == ["workspace/review.md"]
    assert member.metadata_json["artifacts"] == [{"path": "workspace/review.md", "type": "artifact"}]
    assert member.metadata_json["t0_refs"] == ["session#event-1"]
    event = next(item for item in db.added if isinstance(item, AgentTeamEvent))
    assert event.event_type == "member_completed"
    assert event.receiver_member_id == member.id
    assert event.payload_json["summary"] == "review passed"
