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
        return _ScalarOne(self.value if self.executes == 1 else None)


class _SequenceCompletionDB(_DB):
    def __init__(self, values) -> None:
        super().__init__()
        self.values = list(values)
        self.executes = 0

    async def execute(self, _stmt):
        self.executes += 1
        if not self.values:
            return _ScalarOne(None)
        return _ScalarOne(self.values.pop(0))


class _ScalarMany:
    def __init__(self, values) -> None:
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class _ExistingMembersDB(_DB):
    def __init__(self, members) -> None:
        super().__init__()
        self.members = members

    async def execute(self, _stmt):
        return _ScalarMany(self.members)


class _FailingFlushDB(_DB):
    async def flush(self) -> None:
        raise RuntimeError("flush failed")


def test_agent_team_decision_entry_summarizes_members_and_lead_actions():
    from app.services.agent_team_runtime_service import build_agent_team_decision_entry

    team_id = uuid4()
    team = SimpleNamespace(
        id=team_id,
        name="Research Team",
        status="active",
        metadata_json={"open_tasks": [{"id": "todo-1", "title": "Merge findings"}]},
    )
    members = [
        SimpleNamespace(
            id=uuid4(),
            member_name="researcher",
            status="idle",
            runtime_task_id=uuid4(),
            metadata_json={"last_turn_status": "completed", "summary": "report ready"},
        ),
        SimpleNamespace(
            id=uuid4(),
            member_name="critic",
            status="idle",
            runtime_task_id=uuid4(),
            metadata_json={"last_turn_status": "failed", "summary": "verification failed"},
        ),
    ]

    entry = build_agent_team_decision_entry(team, members)

    assert entry["schema"] == "hive.ccplus.agent_team_decision.v1"
    assert entry["team_id"] == str(team_id)
    assert entry["team_outcome"] == "failed"
    assert entry["open_tasks"] == [{"id": "todo-1", "title": "Merge findings"}]
    assert "review_failed_members" in entry["lead_required_actions"]
    assert [member["member_name"] for member in entry["member_statuses"]] == ["researcher", "critic"]
    assert entry["member_statuses"][1]["last_turn_status"] == "failed"


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
    assert payload["team_create_semantics"] == "container_only"
    assert payload["teammate_creation_tool"] == "spawn_subagent"
    assert payload["team_task_list"]["id"] == "Parity Review"
    assert payload["team_task_list"]["owner_field"] == "member_name"
    assert payload["team_task_list"]["claim_tool"] == "track_todo"
    assert payload["teammate_lifecycle"]["idle_after_each_turn"] is True
    assert payload["teammate_lifecycle"]["address_by"] == "member_name"
    assert payload["teammate_creation_args"] == {
        "team_name": "Parity Review",
        "name": "<member-name>",
        "prompt": "<task>",
    }
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
async def test_agenttool_teammate_spawn_reserves_team_session_before_records_and_inherits_budget(monkeypatch):
    from app.services.agent_team_runtime_service import (
        TeamMemberCreateSpec,
        create_agent_team_runtime_result,
        spawn_agent_team_member_runtime,
    )
    from app.services.runtime_budget_service import RuntimeBudgetReservationResult

    db = _DB()
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    parent_session = SimpleNamespace(id=uuid4(), root_session_id=None)
    budget_run_id = uuid4()
    captured: dict = {}

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(event_id=uuid4())

    async def fake_continue_agent_session_from_mailbox(**kwargs):
        captured["continuation"] = kwargs
        return {"ok": True, "status": "queued", "consumer": "teammate_spawn", "run_id": str(uuid4())}

    class BudgetService:
        async def reserve(self, reservation):
            assert not db.added, "admission must happen before member/session records are written"
            captured["reservation"] = reservation
            return RuntimeBudgetReservationResult(
                allowed=True,
                would_deny=False,
                idempotent=False,
                budget_run_id=reservation.budget_run_id,
            )

        async def settle(self, settlement):
            captured["settlement"] = settlement

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
        name="Budget Team",
        members=[],
        source="unit_test",
    )
    db.added.clear()

    payload = await spawn_agent_team_member_runtime(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=created.team,
        spec=TeamMemberCreateSpec(name="critic", role="Review"),
        prompt="Review the work.",
        source="unit_test",
        budget_run_id=budget_run_id,
        budget_service=BudgetService(),
    )

    assert payload["ok"] is True
    assert captured["reservation"].budget_run_id == budget_run_id
    assert captured["reservation"].team_sessions == 1
    assert captured["reservation"].background_tasks == 1
    assert captured["continuation"]["extra_metadata"]["budget_run_id"] == str(budget_run_id)
    assert captured["settlement"].actual_team_sessions == 1
    assert captured["settlement"].actual_background_tasks == 1


@pytest.mark.asyncio
async def test_agenttool_teammate_spawn_waits_for_approval_without_half_created_member(monkeypatch):
    from app.models.agent_team import AgentTeamMember
    from app.models.chat_session import ChatSession
    from app.services.agent_team_runtime_service import TeamMemberCreateSpec, spawn_agent_team_member_runtime
    from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired

    db = _DB()
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    parent_session = SimpleNamespace(id=uuid4(), root_session_id=None)
    team = SimpleNamespace(id=uuid4(), name="Budget Team", parent_session_id=parent_session.id)

    class WaitingBudgetService:
        async def reserve(self, reservation):
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["team_sessions"],
            )

    payload = await spawn_agent_team_member_runtime(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=team,
        spec=TeamMemberCreateSpec(name="critic", role="Review"),
        prompt="Review the work.",
        source="unit_test",
        budget_run_id=uuid4(),
        budget_service=WaitingBudgetService(),
    )

    assert payload["ok"] is False
    assert payload["status"] == "waiting_budget_approval"
    assert not any(isinstance(item, (AgentTeamMember, ChatSession)) for item in db.added)


@pytest.mark.asyncio
async def test_agenttool_teammate_spawn_uses_unique_name_in_budget_reservation(monkeypatch):
    from app.models.agent_team import AgentTeamMember
    from app.services.agent_team_runtime_service import TeamMemberCreateSpec, spawn_agent_team_member_runtime
    from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired

    existing = AgentTeamMember(
        id=uuid4(),
        team_id=uuid4(),
        member_name="critic",
        chat_session_id=uuid4(),
    )
    db = _ExistingMembersDB([existing])
    parent_session = SimpleNamespace(id=uuid4(), root_session_id=None)
    team = SimpleNamespace(id=existing.team_id, name="Budget Team", parent_session_id=parent_session.id)
    captured: dict = {}

    class WaitingBudgetService:
        async def reserve(self, reservation):
            captured["reservation"] = reservation
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["team_sessions"],
            )

    payload = await spawn_agent_team_member_runtime(
        db=db,
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
        parent_session=parent_session,
        team=team,
        spec=TeamMemberCreateSpec(name="critic", role="Review"),
        prompt="Review the work.",
        source="unit_test",
        budget_run_id=uuid4(),
        budget_service=WaitingBudgetService(),
    )

    assert payload["member_name"] == "critic-2"
    assert captured["reservation"].reservation_key.endswith(":critic-2")


@pytest.mark.asyncio
async def test_agenttool_teammate_spawn_releases_admission_when_record_flush_fails():
    from app.services.agent_team_runtime_service import TeamMemberCreateSpec, spawn_agent_team_member_runtime
    from app.services.runtime_budget_service import RuntimeBudgetReservationResult

    db = _FailingFlushDB()
    parent_session = SimpleNamespace(id=uuid4(), root_session_id=None)
    team = SimpleNamespace(id=uuid4(), name="Budget Team", parent_session_id=parent_session.id)
    captured: dict = {}

    class BudgetService:
        async def reserve(self, reservation):
            return RuntimeBudgetReservationResult(
                allowed=True,
                would_deny=False,
                idempotent=False,
                budget_run_id=reservation.budget_run_id,
            )

        async def settle(self, settlement):
            captured["settlement"] = settlement

    with pytest.raises(RuntimeError, match="flush failed"):
        await spawn_agent_team_member_runtime(
            db=db,
            agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
            user=SimpleNamespace(id=uuid4()),
            parent_session=parent_session,
            team=team,
            spec=TeamMemberCreateSpec(name="critic", role="Review"),
            prompt="Review the work.",
            source="unit_test",
            budget_run_id=uuid4(),
            budget_service=BudgetService(),
        )

    assert captured["settlement"].actual_team_sessions == 0
    assert captured["settlement"].actual_background_tasks == 0
    assert captured["settlement"].reason == "agent_team_member_spawn_failed"


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
    assert member.status == "idle"
    assert member.metadata_json["existing"] == "keep"
    assert member.metadata_json["summary"] == "review passed"
    assert member.metadata_json["last_turn_status"] == "completed"
    assert member.metadata_json["idle_after_turn"] is True
    assert member.metadata_json["artifact_paths"] == ["workspace/review.md"]
    assert member.metadata_json["artifacts"] == [{"path": "workspace/review.md", "type": "artifact"}]
    assert member.metadata_json["t0_refs"] == ["session#event-1"]
    event_types = [item.event_type for item in db.added if isinstance(item, AgentTeamEvent)]
    assert "member_completed" in event_types
    assert "member_idle" in event_types
    idle_event = next(
        item for item in db.added if isinstance(item, AgentTeamEvent) and item.event_type == "member_idle"
    )
    assert idle_event.receiver_member_id == member.id
    assert idle_event.payload_json["summary"] == "review passed"
    assert payload["agent_team_decision_entry"]["team_outcome"] == "idle"
    assert payload["agent_team_decision_entry"]["member_statuses"][0]["member_name"] == "critic"


@pytest.mark.asyncio
async def test_team_member_completion_wakes_parent_session_with_task_notification(monkeypatch):
    from app.models.agent_team import AgentTeam, AgentTeamMember
    from app.services.agent_team_runtime_service import project_agent_team_member_completion

    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, creator_id=user.id)
    parent_session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=user.id,
        parent_session_id=None,
        root_session_id=None,
        session_kind="human_chat",
        runtime_source="web_chat",
    )
    team = AgentTeam(
        id=uuid4(),
        tenant_id=tenant_id,
        lead_agent_id=agent.id,
        parent_session_id=parent_session.id,
        name="Research Team",
        created_by_user_id=user.id,
    )
    run_id = uuid4()
    member = AgentTeamMember(
        id=uuid4(),
        team_id=team.id,
        member_name="researcher",
        chat_session_id=uuid4(),
        runtime_task_id=run_id,
        metadata_json={},
    )
    task = SimpleNamespace(
        id=run_id,
        task_type="team_member",
        child_session_id=str(member.chat_session_id),
        metadata_json={"artifact_paths": ["workspace/report.md"]},
    )
    db = _SequenceCompletionDB([member, team, parent_session, agent, user])
    captured = []

    async def fake_continue_parent_session_with_task_notification(**kwargs):
        captured.append(kwargs)
        return {"ok": True, "status": "started"}

    monkeypatch.setattr(
        "app.services.agent_team_runtime_service.continue_parent_session_with_task_notification",
        fake_continue_parent_session_with_task_notification,
    )

    payload = await project_agent_team_member_completion(
        db=db,
        task=task,
        status="completed",
        result_summary="report ready",
        metadata_json=task.metadata_json,
    )

    assert payload is not None
    assert len(captured) == 1
    wake = captured[0]
    assert wake["agent"] is agent
    assert wake["user"] is user
    assert wake["session"] is parent_session
    assert wake["task_id"] == str(run_id)
    assert wake["task_type"] == "team_member"
    assert wake["status"] == "completed"
    assert wake["source"] == "agent_team"
    assert wake["child_session_id"] == str(member.chat_session_id)
    assert wake["child_agent_name"] == "researcher"
    assert "report ready" in wake["summary"]
    assert member.metadata_json["parent_task_notification_side_effect"]["source"] == "agent_team"
