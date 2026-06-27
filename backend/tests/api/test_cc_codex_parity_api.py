from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.added = []
        self.flushes = 0
        self.refreshes = []
        self.commits = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def refresh(self, value):
        self.refreshes.append(value)

    async def commit(self):
        self.commits += 1


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


class _FilteringExecuteDB(_FakeDB):
    def __init__(self, values_by_model: dict[str, object]) -> None:
        super().__init__()
        self.values_by_model = values_by_model
        self.executes = 0

    async def execute(self, stmt):
        self.executes += 1
        text = str(stmt)
        aliases = {
            "ChatSession": ("ChatSession", "chat_sessions"),
            "AgentSessionGoal": ("AgentSessionGoal", "agent_session_goals"),
            "AgentTeam": ("AgentTeam", "agent_teams"),
            "AgentTeamMember": ("AgentTeamMember", "agent_team_members"),
        }
        for model_name, value in self.values_by_model.items():
            candidates = aliases.get(model_name, (model_name,))
            if any(candidate in text for candidate in candidates):
                return _ScalarResult(value)
        return _ScalarResult(None)


@pytest.mark.asyncio
async def test_commands_api_lists_compact_index_and_schema(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_pack_policies(_db, requested_tenant_id, requested_agent_id):
        assert requested_tenant_id == tenant_id
        assert requested_agent_id == agent_id
        return {"coding_pack": True}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_pack_policies", fake_pack_policies)

    index = await commands_api.list_agent_commands(agent_id=agent_id, current_user=current_user, db=db)
    assert any(item["name"] == "goal_start" for item in index)
    assert all("input_schema" not in item for item in index)
    assert any(item["name"] == "diff" and item["category"] == "coding_pack" for item in index)

    user_index = await commands_api.list_agent_commands(
        agent_id=agent_id,
        surface="user",
        include_optional_packs=True,
        current_user=current_user,
        db=db,
    )
    user_command_names = {item["name"] for item in user_index}
    assert {
        "plan",
        "goal",
        "task",
        "schedule",
        "once",
        "team",
        "agent",
        "skill",
        "workflow",
        "mcp",
        "permissions",
        "clear",
        "compact",
        "context",
        "usage",
        "resume",
        "branch",
        "rewind",
    }.issubset(user_command_names)
    assert {
        "team_create",
        "team_delete",
        "task_create",
        "task_get",
        "task_list",
        "task_output",
        "task_stop",
        "task_update",
        "goal_start",
        "goal_update",
        "goal_stop",
        "schedule_create",
        "schedule_once",
        "advanced_plan",
        "verify_plan",
        "load_skill",
        "preview_workflow",
        "start_workflow",
        "diff",
        "commit",
        "status",
        "cost",
        "stats",
        "doctor",
        "version",
    }.isdisjoint(user_command_names)
    assert next(item for item in user_index if item["name"] == "team")["canonical_name"] == "team_create"
    assert next(item for item in user_index if item["name"] == "task")["canonical_name"] == "task_create"
    assert next(item for item in user_index if item["name"] == "goal")["canonical_name"] == "goal_start"
    assert next(item for item in user_index if item["name"] == "schedule")["canonical_name"] == "schedule_create"
    assert next(item for item in user_index if item["name"] == "once")["canonical_name"] == "schedule_once"

    schema = await commands_api.get_agent_command(
        agent_id=agent_id, command_name="goal", current_user=current_user, db=db
    )
    assert schema["name"] == "goal_start"
    assert schema["input_schema"]["properties"]["objective"]["type"] == "string"

    schedule_schema = await commands_api.get_agent_command(
        agent_id=agent_id, command_name="schedule", current_user=current_user, db=db
    )
    assert schedule_schema["name"] == "schedule_create"
    assert schedule_schema["category"] == "schedule"
    assert schedule_schema["input_schema"]["properties"]["cron_expr"]["type"] == "string"

    once_schema = await commands_api.get_agent_command(
        agent_id=agent_id, command_name="once", current_user=current_user, db=db
    )
    assert once_schema["name"] == "schedule_once"
    assert once_schema["category"] == "schedule"
    assert once_schema["input_schema"]["properties"]["at"]["type"] == "string"

    alias_schema = await commands_api.get_agent_command(
        agent_id=agent_id, command_name="team", current_user=current_user, db=db
    )
    assert alias_schema["name"] == "team_create"
    assert alias_schema["aliases"] == ["team"]


@pytest.mark.asyncio
async def test_commands_api_schema_endpoint_uses_user_visible_names(monkeypatch):
    import app.api.commands as commands_api
    from fastapi import HTTPException

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)

    goal_schema = await commands_api.get_agent_command(
        agent_id=agent_id,
        command_name="goal",
        current_user=current_user,
        db=db,
    )
    assert goal_schema["name"] == "goal_start"

    for hidden_or_canonical in (
        "goal_start",
        "team_create",
        "task_create",
        "schedule_create",
        "schedule_once",
        "copy",
        "export",
        "btw",
        "turn_steer",
        "rollback",
        "rename",
        "tag",
        "interrupt",
        "checkpoints",
    ):
        with pytest.raises(HTTPException) as exc:
            await commands_api.get_agent_command(
                agent_id=agent_id,
                command_name=hidden_or_canonical,
                current_user=current_user,
                db=db,
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_commands_api_hides_optional_coding_pack_without_policy(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    async def fake_pack_policies(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_pack_policies", fake_pack_policies)

    index = await commands_api.list_agent_commands(
        agent_id=agent_id,
        surface="user",
        include_optional_packs=True,
        current_user=current_user,
        db=db,
    )

    assert all(item["name"] != "diff" for item in index)


@pytest.mark.asyncio
async def test_commands_api_adds_installed_skills_as_user_invocable_commands(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    async def fake_extensions(_db, requested_agent_id):
        assert requested_agent_id == agent_id
        return {
            "skills": [
                {"id": "market-research", "name": "market-research", "source": "workspace", "status": "available"},
                {"id": "bad name", "name": "bad name", "source": "workspace", "status": "available"},
            ],
            "mcp_servers": [],
            "plugins": [],
        }

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_extensions", fake_extensions)

    index = await commands_api.list_agent_commands(
        agent_id=agent_id,
        surface="user",
        current_user=current_user,
        db=db,
    )

    skill = next(item for item in index if item["name"] == "market-research")
    assert skill["category"] == "skill"
    assert skill["source"] == "skill"
    assert skill["canonical_name"] == "market-research"
    assert all(item["name"] != "bad name" for item in index)


@pytest.mark.asyncio
async def test_chat_sessions_api_exposes_session_index(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()
    captured = {}

    async def fake_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    async def fake_read_session_index(db_arg, *, agent_id, session_id):
        captured.update({"db": db_arg, "agent_id": agent_id, "session_id": session_id})
        return {
            "schema": "hive.session_index.v1",
            "session_id": str(session_id),
            "checkpoints": [{"checkpoint_event_id": "event-1"}],
            "dynamic_tools": ["web_search"],
        }

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_access)
    monkeypatch.setattr(chat_sessions_api, "read_session_index", fake_read_session_index)

    result = await chat_sessions_api.get_session_index(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )

    assert result["schema"] == "hive.session_index.v1"
    assert result["checkpoints"][0]["checkpoint_event_id"] == "event-1"
    assert captured == {"db": db, "agent_id": agent_id, "session_id": session_id}


@pytest.mark.asyncio
async def test_chat_sessions_api_exposes_unified_workbench_and_json_export(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=current_user.id, title="Launch sync")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    db = _FakeDB()
    captured = []

    async def fake_get_run_session_and_agent(**kwargs):
        captured.append(("access", kwargs))
        return session, agent, "manage"

    async def fake_build_session_workbench(db_arg, *, agent, session):
        captured.append(("workbench", {"db": db_arg, "agent": agent, "session": session}))
        return {
            "schema": "hive.ccplus.session_workbench.v1",
            "session": {"id": str(session.id), "title": session.title},
            "turn": {"truth_source": "t0_events_jsonl", "event_count": 2},
            "controls": {"can_export_json": True},
        }

    async def fake_build_session_json_export(db_arg, *, agent, session):
        captured.append(("export", {"db": db_arg, "agent": agent, "session": session}))
        return {
            "schema": "hive.ccplus.session_export.v1",
            "session": {"id": str(session.id), "title": session.title},
            "transcript": {"truth_source": "t0_events_jsonl", "events": [{"sequence": 1}]},
        }

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "build_session_workbench", fake_build_session_workbench)
    monkeypatch.setattr(chat_sessions_api, "build_session_json_export", fake_build_session_json_export)

    workbench = await chat_sessions_api.get_session_workbench(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )
    exported = await chat_sessions_api.export_session_json(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )

    assert workbench["schema"] == "hive.ccplus.session_workbench.v1"
    assert workbench["turn"]["truth_source"] == "t0_events_jsonl"
    assert exported["schema"] == "hive.ccplus.session_export.v1"
    assert exported["transcript"]["events"][0]["sequence"] == 1
    assert [item[0] for item in captured] == ["access", "workbench", "access", "export"]


@pytest.mark.asyncio
async def test_commands_api_rejects_internal_tool_commands_from_web(monkeypatch):
    import app.api.commands as commands_api
    from fastapi import HTTPException

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)

    for command_name in ("advanced_plan", "load_skill", "start_workflow", "preview_workflow", "goal_start"):
        with pytest.raises(HTTPException) as exc:
            await commands_api.execute_agent_command(
                agent_id=agent_id,
                command_name=command_name,
                body=commands_api.ExecuteCommandIn(arguments={"objective": "Plan the rollout"}),
                current_user=current_user,
                db=db,
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_commands_api_allows_internal_tool_commands_from_agent_origin(monkeypatch):
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
        body=commands_api.ExecuteCommandIn(arguments={"objective": "Plan the rollout"}, origin="agent"),
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
async def test_commands_api_executes_session_command_without_tool_runtime(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    db = _FakeDB()
    captured = {}

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    async def fake_execute_session_command(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "title": "Renamed"}

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("session metadata commands must not execute as LLM tools")

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_session_command", fake_execute_session_command)
    monkeypatch.setattr(commands_api, "execute_tool", fail_execute_tool)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="rename",
        body=commands_api.ExecuteCommandIn(
            arguments={"title": "Renamed"},
            session_id=str(session_id),
            origin="agent",
        ),
        current_user=current_user,
        db=db,
    )

    assert result == {"ok": True, "command": "rename", "result": {"ok": True, "title": "Renamed"}}
    assert db.commits == 1
    assert captured["agent"] is agent
    assert captured["user"] is current_user
    assert captured["access_level"] == "manage"
    assert captured["command_name"] == "rename"
    assert captured["session_id"] == str(session_id)
    assert captured["arguments"] == {"title": "Renamed"}


@pytest.mark.asyncio
async def test_commands_api_executes_diagnostic_command_without_tool_runtime(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    db = _FakeDB()
    captured = {}

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "use"

    async def fake_execute_diagnostic_command(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "command": "version", "version": "1.7.0"}

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("diagnostic commands must not execute as LLM tools")

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_diagnostic_command", fake_execute_diagnostic_command)
    monkeypatch.setattr(commands_api, "execute_tool", fail_execute_tool)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="version",
        body=commands_api.ExecuteCommandIn(arguments={}, session_id="session-1", origin="agent"),
        current_user=current_user,
        db=db,
    )

    assert result == {
        "ok": True,
        "command": "version",
        "result": {"ok": True, "command": "version", "version": "1.7.0"},
    }
    assert captured["db"] is db
    assert captured["agent"] is agent
    assert captured["user"] is current_user
    assert captured["command_name"] == "version"
    assert captured["session_id"] == "session-1"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_commands_api_goal_lifecycle_is_durable_not_requires_api_persist(monkeypatch):
    import app.api.commands as commands_api
    from app.models.agent_session_goal import AgentSessionGoal

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    goal_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("goal lifecycle commands must persist directly through the command runtime")

    recorded_events: list[dict] = []

    async def fake_append_session_event(**kwargs):
        recorded_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_tool", fail_execute_tool)
    monkeypatch.setattr(commands_api, "append_session_event", fake_append_session_event)

    start_db = _FilteringExecuteDB({"ChatSession": SimpleNamespace(id=session_id, agent_id=agent_id)})
    started = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="goal_start",
        body=commands_api.ExecuteCommandIn(
            arguments={"objective": "Finish parity", "token_budget": 1000},
            session_id=str(session_id),
            origin="agent",
        ),
        current_user=current_user,
        db=start_db,
    )
    assert started["ok"] is True
    assert started["result"]["requires_api_persist"] is False
    stored_goal = next(item for item in start_db.added if isinstance(item, AgentSessionGoal))
    assert stored_goal.objective == "Finish parity"
    assert stored_goal.chat_session_id == session_id
    assert stored_goal.created_by_user_id == current_user.id
    assert start_db.commits == 1
    assert recorded_events[-1]["event_type"] == "goal"
    assert recorded_events[-1]["metadata"]["goal_id"] == str(stored_goal.id)
    assert recorded_events[-1]["metadata"]["status"] == "active"

    goal = AgentSessionGoal(
        id=goal_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Finish parity",
        token_budget=1000,
    )
    update_db = _FilteringExecuteDB({"AgentSessionGoal": goal})
    updated = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="goal_update",
        body=commands_api.ExecuteCommandIn(
            arguments={"objective": "Finish all parity", "token_budget": 1500, "max_continuation_turns": 3},
            session_id=str(session_id),
            origin="agent",
        ),
        current_user=current_user,
        db=update_db,
    )
    assert updated["result"]["objective"] == "Finish all parity"
    assert goal.objective == "Finish all parity"
    assert goal.token_budget == 1500
    assert goal.max_continuation_turns == 3
    assert recorded_events[-1]["event_type"] == "goal"
    assert recorded_events[-1]["metadata"]["status"] == "active"

    stop_db = _FilteringExecuteDB({"AgentSessionGoal": goal})
    stopped = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="goal_stop",
        body=commands_api.ExecuteCommandIn(
            arguments={"status": "complete", "completion_summary": "Done"},
            session_id=str(session_id),
            origin="agent",
        ),
        current_user=current_user,
        db=stop_db,
    )
    assert stopped["result"]["status"] == "complete"
    assert goal.status == "complete"
    assert goal.completion_summary == "Done"
    assert recorded_events[-1]["event_type"] == "goal"
    assert recorded_events[-1]["metadata"]["status"] == "complete"


@pytest.mark.asyncio
async def test_commands_api_schedule_create_persists_disabled_draft_without_tool_runtime(monkeypatch):
    import app.api.commands as commands_api
    from app.models.trigger import AgentTrigger

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=current_user.id)
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    def fake_is_creator(user, requested_agent):
        assert user is current_user
        assert requested_agent is agent
        return True

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("schedule_create must persist through the command runtime")

    async def fail_enforce_plan_gate(*_args, **_kwargs):
        raise AssertionError("disabled schedule drafts must not require Plan Mode")

    recorded_events: list[dict] = []

    async def fake_append_session_event(**kwargs):
        recorded_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "is_agent_creator", fake_is_creator)
    monkeypatch.setattr(commands_api, "execute_tool", fail_execute_tool)
    monkeypatch.setattr(commands_api, "enforce_plan_gate", fail_enforce_plan_gate)
    monkeypatch.setattr(commands_api, "append_session_event", fake_append_session_event)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="schedule",
        body=commands_api.ExecuteCommandIn(
            arguments={
                "name": "daily_briefing",
                "instruction": "Prepare the daily briefing.",
                "cron_expr": "0 9 * * *",
                "is_enabled": False,
            },
            session_id=str(session_id),
        ),
        current_user=current_user,
        db=db,
    )

    assert result["ok"] is True
    assert result["command"] == "schedule_create"
    assert result["result"]["name"] == "daily_briefing"
    assert result["result"]["is_enabled"] is False
    stored_trigger = next(item for item in db.added if isinstance(item, AgentTrigger))
    assert stored_trigger.type == "cron"
    assert stored_trigger.config["expr"] == "0 9 * * *"
    assert stored_trigger.config["trigger_class"] == "scheduled_job"
    assert stored_trigger.config["command"] == "schedule_create"
    assert stored_trigger.config["created_by"] == str(current_user.id)
    assert db.commits == 1
    assert recorded_events[-1]["event_type"] == "schedule"
    assert recorded_events[-1]["metadata"]["schedule_id"] == str(stored_trigger.id)
    assert recorded_events[-1]["metadata"]["status"] == "created"


@pytest.mark.asyncio
async def test_commands_api_schedule_once_persists_disabled_draft_without_tool_runtime(monkeypatch):
    import app.api.commands as commands_api
    from app.models.trigger import AgentTrigger

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=current_user.id)
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    def fake_is_creator(user, requested_agent):
        assert user is current_user
        assert requested_agent is agent
        return True

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("schedule_once must persist through the command runtime")

    async def fail_enforce_plan_gate(*_args, **_kwargs):
        raise AssertionError("disabled one-shot schedule drafts must not require Plan Mode")

    recorded_events: list[dict] = []

    async def fake_append_session_event(**kwargs):
        recorded_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "is_agent_creator", fake_is_creator)
    monkeypatch.setattr(commands_api, "execute_tool", fail_execute_tool)
    monkeypatch.setattr(commands_api, "enforce_plan_gate", fail_enforce_plan_gate)
    monkeypatch.setattr(commands_api, "append_session_event", fake_append_session_event)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="once",
        body=commands_api.ExecuteCommandIn(
            arguments={
                "name": "one_time_audit",
                "instruction": "Run the one-time audit.",
                "at": "2026-06-26T09:00:00Z",
                "is_enabled": False,
            },
            session_id=str(session_id),
        ),
        current_user=current_user,
        db=db,
    )

    assert result["ok"] is True
    assert result["command"] == "schedule_once"
    assert result["result"]["name"] == "one_time_audit"
    assert result["result"]["is_enabled"] is False
    assert result["result"]["at"] == "2026-06-26T09:00:00+00:00"
    stored_trigger = next(item for item in db.added if isinstance(item, AgentTrigger))
    assert stored_trigger.type == "once"
    assert stored_trigger.config["at"] == "2026-06-26T09:00:00+00:00"
    assert stored_trigger.config["trigger_class"] == "scheduled_job"
    assert stored_trigger.config["command"] == "schedule_once"
    assert stored_trigger.config["created_by"] == str(current_user.id)
    assert db.commits == 1
    assert recorded_events[-1]["event_type"] == "once"
    assert recorded_events[-1]["metadata"]["once_id"] == str(stored_trigger.id)
    assert recorded_events[-1]["metadata"]["status"] == "created"


@pytest.mark.asyncio
async def test_commands_api_team_create_and_delete_are_durable(monkeypatch):
    import app.api.commands as commands_api
    from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
    from app.models.chat_session import ChatSession

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    team_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("team lifecycle commands must persist directly through the command runtime")

    emitted = []
    recorded_events: list[dict] = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event.value, kwargs))

    async def fake_append_session_event(**kwargs):
        recorded_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_tool", fail_execute_tool)
    monkeypatch.setattr(commands_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr("app.services.agent_team_runtime_service.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.services.agent_team_runtime_service.append_session_event", fake_append_session_event)

    create_db = _FilteringExecuteDB({"ChatSession": SimpleNamespace(id=session_id, agent_id=agent_id)})
    created = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="team",
        body=commands_api.ExecuteCommandIn(
            arguments={"name": "research", "members": [{"name": "critic", "role": "Review"}]},
            session_id=str(session_id),
        ),
        current_user=current_user,
        db=create_db,
    )

    assert created["result"]["requires_api_persist"] is False
    assert created["result"]["status"] == "active"
    assert {type(item).__name__ for item in create_db.added} >= {
        "AgentTeam",
        "AgentTeamMember",
        "AgentTeamEvent",
        "ChatSession",
    }
    team = next(item for item in create_db.added if isinstance(item, AgentTeam))
    member = next(item for item in create_db.added if isinstance(item, AgentTeamMember))
    member_session = next(item for item in create_db.added if isinstance(item, ChatSession))
    assert team.parent_session_id == session_id
    assert member.team_id == team.id
    assert member.chat_session_id == member_session.id
    assert emitted[0][0] == "team_created"
    assert recorded_events[-1]["event_type"] == "team_member"
    assert recorded_events[-1]["metadata"]["team_id"] == str(team.id)
    assert recorded_events[-1]["metadata"]["child_session_id"] == str(member_session.id)

    existing_team = AgentTeam(
        id=team_id,
        tenant_id=tenant_id,
        lead_agent_id=agent_id,
        parent_session_id=session_id,
        name="research",
    )
    existing_member = AgentTeamMember(id=uuid4(), team_id=team_id, member_name="critic", chat_session_id=uuid4())
    delete_db = _FilteringExecuteDB({"AgentTeam": existing_team, "AgentTeamMember": [existing_member]})
    deleted = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="team_delete",
        body=commands_api.ExecuteCommandIn(
            arguments={"team_id": str(team_id)}, session_id=str(session_id), origin="agent"
        ),
        current_user=current_user,
        db=delete_db,
    )
    assert deleted["result"]["status"] == "closed"
    assert existing_team.status == "closed"
    assert existing_member.status == "closed"
    assert any(isinstance(item, AgentTeamEvent) and item.event_type == "team_closed" for item in delete_db.added)
    assert recorded_events[-1]["event_type"] == "team_member"
    assert recorded_events[-1]["metadata"]["status"] == "closed"


@pytest.mark.asyncio
async def test_commands_api_bridges_skill_workflow_mcp_config_and_permissions(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    db = _FakeDB()
    captured_tools = []

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "use"

    async def fake_execute_tool(tool_name, arguments, *, agent_id, user_id, session_id=None, **_kwargs):
        captured_tools.append((tool_name, arguments, session_id))
        return '{"ok": true, "tool_name": "%s"}' % tool_name

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_tool", fake_execute_tool)

    for command_name, arguments in [
        ("load_skill", {"skill_name": "research"}),
        ("preview_workflow", {"workflow_ref": "wf"}),
        ("start_workflow", {"workflow_ref": "wf"}),
        ("mcp", {"action": "list_tools"}),
    ]:
        result = await commands_api.execute_agent_command(
            agent_id=agent_id,
            command_name=command_name,
            body=commands_api.ExecuteCommandIn(arguments=arguments, session_id="session-1", origin="agent"),
            current_user=current_user,
            db=db,
        )
        assert result["ok"] is True
        assert result["result"]["ok"] is True

    config = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="config",
        body=commands_api.ExecuteCommandIn(arguments={}, session_id="session-1", origin="agent"),
        current_user=current_user,
        db=db,
    )
    assert config["result"]["command"] == "config"
    assert config["result"]["mode"] == "read_only_runtime_view"

    permissions = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="permissions",
        body=commands_api.ExecuteCommandIn(arguments={}, session_id="session-1"),
        current_user=current_user,
        db=db,
    )
    assert permissions["result"]["command"] == "permissions"
    assert permissions["result"]["access_level"] == "use"
    assert ("load_skill", {"name": "research"}, "session-1") in captured_tools
    assert ("list_mcp_tools", {}, "session-1") in captured_tools


@pytest.mark.asyncio
async def test_commands_api_web_product_commands_return_prompt_or_navigation_actions(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    db = _FakeDB()
    captured_tools = []

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "use"

    async def fake_execute_tool(tool_name, arguments, *, agent_id, user_id, session_id=None, **_kwargs):
        captured_tools.append((tool_name, arguments, session_id))
        return "Loaded research skill body."

    async def fake_extensions(_db, requested_agent_id):
        assert requested_agent_id == agent_id
        return {
            "skills": [{"id": "research", "name": "research", "source": "workspace", "status": "available"}],
            "mcp_servers": [],
            "plugins": [],
        }

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(commands_api, "get_agent_extensions", fake_extensions)

    skill = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="research",
        body=commands_api.ExecuteCommandIn(arguments={"input": "summarize this"}, session_id="session-1"),
        current_user=current_user,
        db=db,
    )
    assert skill["result"]["action"] == "chat_prompt"
    assert "Loaded research skill body." in skill["result"]["content"]
    assert ("load_skill", {"name": "research"}, "session-1") in captured_tools

    workflow = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="workflow",
        body=commands_api.ExecuteCommandIn(arguments={"input": "triage incoming leads"}, session_id="session-1"),
        current_user=current_user,
        db=db,
    )
    assert workflow["result"] == {
        "ok": True,
        "action": "open_tab",
        "tab": "workflows",
        "panel": "dynamic_workflow",
        "draft": "triage incoming leads",
        "message": "Opened Dynamic Workflow.",
    }

    agent_result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="agent",
        body=commands_api.ExecuteCommandIn(
            arguments={"agent_name": "Researcher", "message": "Collect evidence"},
            session_id="session-1",
        ),
        current_user=current_user,
        db=db,
    )
    assert agent_result["ok"] is True
    assert (
        "delegate_to_agent",
        {"agent_name": "Researcher", "message": "Collect evidence"},
        "session-1",
    ) in captured_tools


@pytest.mark.asyncio
async def test_commands_api_keeps_task_surface_unified_while_routing_internal_flavors(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    db = _FakeDB()
    captured_tools = []

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "use"

    async def fake_execute_tool(tool_name, arguments, *, agent_id, user_id, session_id=None, **_kwargs):
        captured_tools.append((tool_name, arguments, session_id))
        return {"ok": True, "tool_name": tool_name, "arguments": arguments}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_tool", fake_execute_tool)

    cases = [
        ("task_create", {"subject": "Inspect hooks"}, "task_create", {"subject": "Inspect hooks"}),
        (
            "task_create",
            {"kind": "delegation", "agent_name": "Researcher", "message": "Collect evidence"},
            "delegate_to_agent",
            {"agent_name": "Researcher", "message": "Collect evidence"},
        ),
        ("task_list", {"kind": "delegation"}, "list_async_tasks", {}),
        ("task_get", {"kind": "delegation", "task_id": "async-1"}, "check_async_task", {"task_id": "async-1"}),
        ("task_stop", {"kind": "delegation", "task_id": "async-1"}, "cancel_async_task", {"task_id": "async-1"}),
        ("task_output", {"runtime_task_id": "rt-1"}, "task_output", {"runtime_task_id": "rt-1"}),
        ("task_stop", {"runtime_task_id": "rt-1"}, "task_stop", {"runtime_task_id": "rt-1"}),
    ]

    for command_name, arguments, expected_tool, expected_arguments in cases:
        result = await commands_api.execute_agent_command(
            agent_id=agent_id,
            command_name=command_name,
            body=commands_api.ExecuteCommandIn(arguments=arguments, session_id="session-1", origin="agent"),
            current_user=current_user,
            db=db,
        )
        assert result["ok"] is True
        assert result["command"] == command_name
        assert result["result"]["tool_name"] == expected_tool
        assert (expected_tool, expected_arguments, "session-1") in captured_tools


@pytest.mark.asyncio
async def test_commands_api_enforces_bridge_and_remote_safety(monkeypatch):
    import app.api.commands as commands_api
    from fastapi import HTTPException

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "manage"

    async def fake_pack_policies(*_args, **_kwargs):
        return {"coding_pack": True}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_pack_policies", fake_pack_policies)

    with pytest.raises(HTTPException) as bridge_exc:
        await commands_api.execute_agent_command(
            agent_id=agent_id,
            command_name="diff",
            body=commands_api.ExecuteCommandIn(arguments={}, origin="bridge"),
            current_user=current_user,
            db=db,
        )
    assert bridge_exc.value.status_code == 403

    with pytest.raises(HTTPException) as remote_exc:
        await commands_api.execute_agent_command(
            agent_id=agent_id,
            command_name="diff",
            body=commands_api.ExecuteCommandIn(arguments={}, origin="remote"),
            current_user=current_user,
            db=db,
        )
    assert remote_exc.value.status_code == 403


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
    async def fake_load_parent_session(*_args, **_kwargs):
        return SimpleNamespace(id=parent_session_id, root_session_id=parent_session_id)

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr(teams_api, "_load_member_parent_session_or_404", fake_load_parent_session)
    monkeypatch.setattr("app.services.agent_team_runtime_service.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.services.agent_team_runtime_service.append_session_event", fake_append_session_event)

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
    assert {type(item).__name__ for item in db.added} == {
        "AgentTeam",
        "AgentTeamMember",
        "AgentTeamEvent",
        "ChatSession",
    }


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


@pytest.mark.asyncio
async def test_agent_team_api_exposes_workbench(monkeypatch):
    import app.api.agent_teams as teams_api
    from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember

    agent_id = uuid4()
    parent_session_id = uuid4()
    tenant_id = uuid4()
    team_id = uuid4()
    member_id = uuid4()
    member_session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()
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
        metadata_json={"summary": "Found a hook gap.", "t0_refs": ["t0://critic/1"]},
    )
    event = AgentTeamEvent(
        id=uuid4(),
        team_id=team_id,
        sender_member_id=member_id,
        event_type="member_report",
        payload_json={"status": "ready"},
    )

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_load_team(_db, *, agent_id, team_id):
        assert agent_id == team.lead_agent_id
        assert team_id == team.id
        return team

    async def fake_load_members(_db, *, team_id):
        assert team_id == team.id
        return [member]

    async def fake_load_events(_db, *, team_id, limit=200):
        assert team_id == team.id
        assert limit == 200
        return [event]

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)
    monkeypatch.setattr(teams_api, "_load_team_or_404", fake_load_team)
    monkeypatch.setattr(teams_api, "_load_team_members", fake_load_members)
    monkeypatch.setattr(teams_api, "_load_team_events", fake_load_events)

    workbench = await teams_api.get_agent_team_workbench(
        agent_id=agent_id,
        team_id=team_id,
        current_user=current_user,
        db=db,
    )

    assert workbench["schema"] == "hive.ccplus.agent_team_workbench.v1"
    assert workbench["team"]["id"] == str(team_id)
    assert workbench["summary"]["member_count"] == 1
    assert workbench["summary"]["event_count"] == 1
    assert workbench["members"][0]["summary"] == "Found a hook gap."
    assert workbench["events"][0]["event_type"] == "member_report"
