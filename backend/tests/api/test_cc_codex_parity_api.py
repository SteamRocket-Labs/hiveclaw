from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.added = []
        self.flushes = 0
        self.refreshes = []

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def refresh(self, value):
        self.refreshes.append(value)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        value = self._value
        if value is None:
            values = []
        elif isinstance(value, list):
            values = value
        else:
            values = [value]
        return SimpleNamespace(all=lambda: values)


class _ExecuteDB(_FakeDB):
    def __init__(self, *values) -> None:
        super().__init__()
        self._values = list(values)
        self.executes = 0

    async def execute(self, _stmt):
        self.executes += 1
        if not self._values:
            return _ScalarResult(None)
        return _ScalarResult(self._values.pop(0))


@pytest.mark.asyncio
async def test_commands_api_lists_compact_index_and_schema(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)

    index = await commands_api.list_agent_commands(agent_id=agent_id, current_user=current_user, db=db)
    assert any(item["name"] == "goal_start" for item in index)
    assert all("input_schema" not in item for item in index)

    schema = await commands_api.get_agent_command(
        agent_id=agent_id, command_name="goal_start", current_user=current_user, db=db
    )
    assert schema["name"] == "goal_start"
    assert schema["input_schema"]["properties"]["objective"]["type"] == "string"


@pytest.mark.asyncio
async def test_commands_api_executes_builtin_command_tool(monkeypatch, tmp_path):
    import app.api.commands as commands_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)

    captured = {}

    async def fake_execute_tool(tool_name, arguments, *, agent_id, user_id, session_id=None, **_kwargs):
        captured.update(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "agent_id": agent_id,
                "user_id": user_id,
                "session_id": session_id,
            }
        )
        return '{"ok": true, "runtime_task_type": "advanced_plan"}'

    monkeypatch.setattr(commands_api, "execute_tool", fake_execute_tool)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="advanced_plan",
        body=commands_api.ExecuteCommandIn(arguments={"objective": "Plan the rollout"}),
        current_user=current_user,
        db=db,
    )

    assert result["ok"] is True
    assert result["command"] == "advanced_plan"
    assert result["result"]["runtime_task_type"] == "advanced_plan"
    assert captured == {
        "tool_name": "advanced_plan",
        "arguments": {"objective": "Plan the rollout"},
        "agent_id": agent_id,
        "user_id": current_user.id,
        "session_id": None,
    }


@pytest.mark.asyncio
async def test_goals_api_creates_session_goal(monkeypatch):
    import app.api.session_goals as goals_api

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    monkeypatch.setattr(goals_api, "check_agent_access", fake_access)

    result = await goals_api.start_session_goal(
        agent_id=agent_id,
        session_id=session_id,
        body=goals_api.StartGoalIn(objective="Finish all parity work.", token_budget=500, max_continuation_turns=5),
        current_user=current_user,
        db=db,
    )

    assert db.flushes == 1
    stored = db.added[0]
    assert stored.agent_id == agent_id
    assert stored.chat_session_id == session_id
    assert stored.tenant_id == tenant_id
    assert stored.created_by_user_id == current_user.id
    assert result["status"] == "active"
    assert result["objective"] == "Finish all parity work."


@pytest.mark.asyncio
async def test_goals_api_continues_session_goal(monkeypatch):
    import app.api.session_goals as goals_api
    from app.models.agent_session_goal import AgentSessionGoal

    agent_id = uuid4()
    session_id = uuid4()
    goal_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    goal = AgentSessionGoal(
        id=goal_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Continue goal",
    )
    session = SimpleNamespace(id=session_id)
    db = _ExecuteDB(goal, session)
    captured = {}

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "use"

    async def fake_continue_session_goal(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "goal_id": str(goal_id), "run": {"run_id": "run-1"}}

    monkeypatch.setattr(goals_api, "check_agent_access", fake_access)
    monkeypatch.setattr(goals_api, "continue_session_goal", fake_continue_session_goal)

    result = await goals_api.continue_goal(
        agent_id=agent_id,
        session_id=session_id,
        goal_id=goal_id,
        current_user=current_user,
        db=db,
    )

    assert result["ok"] is True
    assert result["run"]["run_id"] == "run-1"
    assert captured["agent"] is agent
    assert captured["user"] is current_user
    assert captured["session"] is session
    assert captured["goal"] is goal
    assert db.executes == 2


@pytest.mark.asyncio
async def test_agent_teams_api_creates_control_index_and_member_sessions(monkeypatch):
    import app.api.agent_teams as teams_api

    agent_id = uuid4()
    parent_session_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)

    result = await teams_api.create_agent_team(
        agent_id=agent_id,
        body=teams_api.CreateAgentTeamIn(
            parent_session_id=parent_session_id,
            name="research",
            members=[teams_api.CreateAgentTeamMemberIn(name="critic", role="Review")],
        ),
        current_user=current_user,
        db=db,
    )

    assert result["name"] == "research"
    assert result["status"] == "active"
    assert result["transcript_truth"] == "chat_session_t0"
    assert result["members"][0]["member_name"] == "critic"
    assert result["members"][0]["runtime_task_type"] == "team_member"
    assert db.flushes == 1
    assert {type(item).__name__ for item in db.added} == {"AgentTeam", "AgentTeamMember", "ChatSession"}


@pytest.mark.asyncio
async def test_agent_teams_api_lists_enters_and_closes_team(monkeypatch):
    import app.api.agent_teams as teams_api
    from app.models.agent_team import AgentTeam, AgentTeamMember

    agent_id = uuid4()
    parent_session_id = uuid4()
    tenant_id = uuid4()
    team_id = uuid4()
    member_id = uuid4()
    member_session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    team = AgentTeam(
        id=team_id,
        tenant_id=tenant_id,
        lead_agent_id=agent_id,
        parent_session_id=parent_session_id,
        name="research",
    )
    member = AgentTeamMember(
        id=member_id,
        team_id=team_id,
        member_name="critic",
        member_role="Review",
        chat_session_id=member_session_id,
        runtime_task_id=uuid4(),
        metadata_json={"t0_refs": ["t0://critic/1"], "summary": "Found gaps."},
    )
    db = _ExecuteDB([team], [member], team, member, team, [member])

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)

    listed = await teams_api.list_agent_teams(
        agent_id=agent_id,
        parent_session_id=parent_session_id,
        current_user=current_user,
        db=db,
    )
    assert listed[0]["id"] == str(team_id)
    assert listed[0]["members"][0]["chat_session_id"] == str(member_session_id)

    entered = await teams_api.enter_agent_team_member(
        agent_id=agent_id,
        team_id=team_id,
        member_id=member_id,
        current_user=current_user,
        db=db,
    )
    assert entered["chat_session_id"] == str(member_session_id)
    assert entered["runtime_task_type"] == "team_member"

    closed = await teams_api.close_agent_team(
        agent_id=agent_id,
        team_id=team_id,
        current_user=current_user,
        db=db,
    )
    assert closed["status"] == "closed"
    assert team.status == "closed"
    assert member.status == "closed"
    assert closed["consolidation_plan"]["merge_mode"] == "summary_with_t0_refs"
    assert closed["consolidation_plan"]["member_summaries"][0]["t0_refs"] == ["t0://critic/1"]
