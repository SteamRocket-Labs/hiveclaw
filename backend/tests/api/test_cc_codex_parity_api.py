from __future__ import annotations

import asyncio
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


@pytest.fixture(autouse=True)
def _allow_api_session_authority(monkeypatch):
    import app.api.agent_teams as teams_api
    import app.api.commands as commands_api

    async def allow_session(_db, user, **kwargs):
        return SimpleNamespace(
            agent=SimpleNamespace(id=kwargs["agent_id"], tenant_id=getattr(user, "tenant_id", None)),
            session=SimpleNamespace(
                id=kwargs["session_id"],
                user_id=user.id,
                root_session_id=kwargs["session_id"],
            ),
            authority_source="session_owner",
        )

    async def allow_team(*_args, **_kwargs):
        return SimpleNamespace(authority_source="session_owner")

    monkeypatch.setattr(commands_api, "authorize_session_action", allow_session)
    monkeypatch.setattr(teams_api, "authorize_session_action", allow_session)
    monkeypatch.setattr(teams_api, "_authorize_team_action", allow_team)


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

    async def fake_capability_group_policies(_db, requested_tenant_id, requested_agent_id):
        assert requested_tenant_id == tenant_id
        assert requested_agent_id == agent_id
        return {"coding_pack": True}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_capability_group_policies", fake_capability_group_policies)

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
    assert next(item for item in user_index if item["name"] == "steer")["canonical_name"] == "turn_steer"

    schema = await commands_api.get_agent_command(
        agent_id=agent_id, command_name="goal", current_user=current_user, db=db
    )
    assert schema["name"] == "goal_start"
    assert schema["input_schema"]["properties"]["objective"]["type"] == "string"

    schedule_schema = await commands_api.get_agent_command(
        agent_id=agent_id, command_name="schedule", current_user=current_user, db=db
    )
    assert schedule_schema["name"] == "schedule_create"

    steer_schema = await commands_api.get_agent_command(
        agent_id=agent_id, command_name="steer", current_user=current_user, db=db
    )
    assert steer_schema["name"] == "turn_steer"
    assert steer_schema["input_schema"]["required"] == ["content"]
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

    async def fake_capability_group_policies(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_capability_group_policies", fake_capability_group_policies)

    index = await commands_api.list_agent_commands(
        agent_id=agent_id,
        surface="user",
        include_optional_packs=True,
        current_user=current_user,
        db=db,
    )

    assert all(item["name"] != "diff" for item in index)


@pytest.mark.asyncio
async def test_coding_pack_command_execute_returns_local_bridge_contract(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_capability_group_policies(_db, requested_tenant_id, requested_agent_id):
        assert requested_tenant_id == tenant_id
        assert requested_agent_id == agent_id
        return {"coding_pack": True}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_capability_group_policies", fake_capability_group_policies)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="notebook",
        body=commands_api.ExecuteCommandIn(arguments={"path": "analysis.ipynb"}, origin="agent"),
        current_user=current_user,
        db=db,
    )

    payload = result["result"]
    assert result["ok"] is False
    assert payload["capability"] == "coding"
    assert payload["requires_local_bridge"] is True
    assert payload["coding_plugin_required"] is True
    assert payload["command_manifest"]["name"] == "notebook"
    assert "notebook_view" in payload["command_manifest"]["tools"]


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
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=current_user.id)
    db = _ExecuteDB(session)
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
        return session, agent, "session_owner"

    async def fake_build_session_workbench(
        db_arg,
        *,
        agent,
        session,
        timeline_limit=50,
        include=None,
        audience="operator",
    ):
        captured.append(
            (
                "workbench",
                {
                    "db": db_arg,
                    "agent": agent,
                    "session": session,
                    "timeline_limit": timeline_limit,
                    "include": include,
                    "audience": audience,
                },
            )
        )
        return {
            "schema": "hive.ccplus.session_workbench.v1",
            "session": {"id": str(session.id), "title": session.title},
            "turn": {"truth_source": "t0_events_jsonl", "event_count": 2},
            "controls": {"can_export_json": True},
        }

    async def fake_build_session_json_export(db_arg, *, agent, session, audience="operator"):
        captured.append(("export", {"db": db_arg, "agent": agent, "session": session, "audience": audience}))
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
    assert captured[1][1]["audience"] == "user"
    assert captured[3][1]["audience"] == "user"


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
    context = captured["context"]
    assert context.agent is agent
    assert context.user is current_user
    assert context.access_level == "manage"
    assert captured["command_name"] == "rename"
    assert context.session_id == str(session_id)
    assert context.arguments == {"title": "Renamed"}


@pytest.mark.asyncio
async def test_commands_api_finalizes_workspace_restore_after_database_commit(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    db = _FakeDB()
    finalized = []

    async def fake_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_execute_session_command(**_kwargs):
        return {
            "ok": True,
            "workspace_restore": {
                "transaction_id": "restore-tx-1",
                "requires_finalize": True,
            },
        }

    def fake_finalize(**kwargs):
        finalized.append((db.commits, kwargs))
        return True

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_session_command", fake_execute_session_command)
    monkeypatch.setattr(commands_api, "finalize_workspace_restore", fake_finalize)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="rewind",
        body=commands_api.ExecuteCommandIn(
            arguments={"mode": "workspace"},
            session_id=str(session_id),
            origin="agent",
        ),
        current_user=current_user,
        db=db,
    )

    assert db.commits == 1
    assert finalized == [
        (
            1,
            {
                "agent_id": agent_id,
                "transaction_id": "restore-tx-1",
                "commit": True,
            },
        )
    ]
    assert result["result"]["workspace_restore"]["requires_finalize"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (RuntimeError("database commit failed"), RuntimeError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
)
async def test_commands_api_rolls_back_workspace_restore_when_database_commit_fails(
    monkeypatch,
    failure,
    expected_type,
):
    import app.api.commands as commands_api

    class _FailingCommitDB(_FakeDB):
        async def commit(self):
            self.commits += 1
            raise failure

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    db = _FailingCommitDB()
    finalized = []

    async def fake_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_execute_session_command(**_kwargs):
        return {
            "ok": True,
            "workspace_restore": {
                "transaction_id": "restore-tx-2",
                "requires_finalize": True,
            },
        }

    def fake_finalize(**kwargs):
        finalized.append(kwargs)
        return True

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "execute_session_command", fake_execute_session_command)
    monkeypatch.setattr(commands_api, "finalize_workspace_restore", fake_finalize)

    with pytest.raises(expected_type):
        await commands_api.execute_agent_command(
            agent_id=agent_id,
            command_name="rewind",
            body=commands_api.ExecuteCommandIn(
                arguments={"mode": "workspace"},
                session_id=str(session_id),
                origin="agent",
            ),
            current_user=current_user,
            db=db,
        )

    assert finalized == [
        {
            "agent_id": agent_id,
            "transaction_id": "restore-tx-2",
            "commit": False,
        }
    ]


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
        body=commands_api.ExecuteCommandIn(
            arguments={}, session_id="00000000-0000-4000-8000-000000000001", origin="agent"
        ),
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
    assert captured["session_id"] == "00000000-0000-4000-8000-000000000001"
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

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("schedule_create must persist through the command runtime")

    async def fail_enforce_plan_gate(*_args, **_kwargs):
        raise AssertionError("disabled schedule drafts must not require Plan Mode")

    recorded_events: list[dict] = []

    async def fake_append_session_event(**kwargs):
        recorded_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
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

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("schedule_once must persist through the command runtime")

    async def fail_enforce_plan_gate(*_args, **_kwargs):
        raise AssertionError("disabled one-shot schedule drafts must not require Plan Mode")

    recorded_events: list[dict] = []

    async def fake_append_session_event(**kwargs):
        recorded_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
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
async def test_commands_api_team_create_creates_container_only_and_delete_is_durable(monkeypatch):
    import app.api.commands as commands_api
    from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember

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
            arguments={"team_name": "research", "description": "Review implementation options."},
            session_id=str(session_id),
        ),
        current_user=current_user,
        db=create_db,
    )

    assert created["result"]["requires_api_persist"] is False
    assert created["result"]["status"] == "active"
    assert created["result"]["members"] == []
    assert {type(item).__name__ for item in create_db.added} == {"AgentTeam", "AgentTeamEvent"}
    team = next(item for item in create_db.added if isinstance(item, AgentTeam))
    assert team.parent_session_id == session_id
    assert emitted[0][0] == "team_created"
    assert recorded_events == []

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
            body=commands_api.ExecuteCommandIn(
                arguments=arguments, session_id="00000000-0000-4000-8000-000000000001", origin="agent"
            ),
            current_user=current_user,
            db=db,
        )
        assert result["ok"] is True
        assert result["result"]["ok"] is True

    config = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="config",
        body=commands_api.ExecuteCommandIn(
            arguments={}, session_id="00000000-0000-4000-8000-000000000001", origin="agent"
        ),
        current_user=current_user,
        db=db,
    )
    assert config["result"]["command"] == "config"
    assert config["result"]["mode"] == "read_only_runtime_view"

    permissions = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="permissions",
        body=commands_api.ExecuteCommandIn(arguments={}, session_id="00000000-0000-4000-8000-000000000001"),
        current_user=current_user,
        db=db,
    )
    assert permissions["result"]["command"] == "permissions"
    assert permissions["result"]["access_level"] == "use"
    assert permissions["result"]["ui_action"] == {
        "type": "open_permissions_menu",
        "session_id": "00000000-0000-4000-8000-000000000001",
        "message": "Session permissions are ready.",
    }
    assert ("load_skill", {"name": "research"}, "00000000-0000-4000-8000-000000000001") in captured_tools
    assert ("list_mcp_tools", {}, "00000000-0000-4000-8000-000000000001") in captured_tools


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
        body=commands_api.ExecuteCommandIn(
            arguments={"input": "summarize this"}, session_id="00000000-0000-4000-8000-000000000001"
        ),
        current_user=current_user,
        db=db,
    )
    assert skill["result"]["action"] == "chat_prompt"
    assert "Loaded research skill body." in skill["result"]["content"]
    assert ("load_skill", {"name": "research"}, "00000000-0000-4000-8000-000000000001") in captured_tools

    workflow = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="workflow",
        body=commands_api.ExecuteCommandIn(
            arguments={"input": "triage incoming leads"}, session_id="00000000-0000-4000-8000-000000000001"
        ),
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
            session_id="00000000-0000-4000-8000-000000000001",
        ),
        current_user=current_user,
        db=db,
    )
    assert agent_result["ok"] is True
    assert (
        "delegate_to_agent",
        {"agent_name": "Researcher", "message": "Collect evidence"},
        "00000000-0000-4000-8000-000000000001",
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
            body=commands_api.ExecuteCommandIn(
                arguments=arguments, session_id="00000000-0000-4000-8000-000000000001", origin="agent"
            ),
            current_user=current_user,
            db=db,
        )
        assert result["ok"] is True
        assert result["command"] == command_name
        assert result["result"]["tool_name"] == expected_tool
        assert (expected_tool, expected_arguments, "00000000-0000-4000-8000-000000000001") in captured_tools


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

    async def fake_capability_group_policies(*_args, **_kwargs):
        return {"coding_pack": True}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_capability_group_policies", fake_capability_group_policies)

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

    async def fake_authorize(_db, _user, **kwargs):
        assert kwargs["agent_id"] == agent_id
        assert kwargs["session_id"] == session_id
        return SimpleNamespace(
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(id=session_id),
        )

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)

    result = await goals_api.start_session_goal(
        agent_id=agent_id,
        session_id=session_id,
        body=goals_api.StartGoalIn(objective="Finish all parity work.", token_budget=500, max_continuation_turns=5),
        current_user=current_user,
        db=db,
    )

    # The goal row and its typed transcript evidence are both persisted.
    assert db.flushes == 2
    stored = db.added[0]
    assert stored.agent_id == agent_id
    assert stored.chat_session_id == session_id
    assert stored.tenant_id == tenant_id
    assert stored.created_by_user_id == current_user.id
    assert result["status"] == "active"
    assert result["objective"] == "Finish all parity work."


@pytest.mark.asyncio
async def test_goals_api_starts_first_goal_turn_in_the_same_request(monkeypatch):
    import app.api.session_goals as goals_api

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id)
    db = _FakeDB()
    captured = {}

    async def fake_authorize(_db, _user, **_kwargs):
        return SimpleNamespace(agent=agent, session=session)

    async def fake_start_web_chat_run(**kwargs):
        captured.update(kwargs)
        return {"run_id": "run-1", "status": "pending"}

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)
    monkeypatch.setattr(goals_api, "start_web_chat_run", fake_start_web_chat_run)

    result = await goals_api.start_session_goal(
        agent_id=agent_id,
        session_id=session_id,
        body=goals_api.StartGoalIn(
            objective="Finish all parity work.",
            content="Full model-visible request",
            display_content="Finish all parity work.",
            attachments=[{"path": "workspace/brief.md"}],
            start_immediately=True,
        ),
        current_user=current_user,
        db=db,
    )

    assert result["run"] == {"run_id": "run-1", "status": "pending"}
    assert captured["agent"] is agent
    assert captured["session"] is session
    assert captured["content"] == "Full model-visible request"
    assert captured["display_content"] == "Finish all parity work."
    assert captured["attachments"] == [{"path": "workspace/brief.md"}]
    assert captured["runtime_task_type"] == "web_chat_turn"
    assert captured["budget_interactive"] is False
    assert captured["extra_metadata"]["goal_id"] == result["id"]
    assert db.added[0].metadata_json["last_goal_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_goals_api_replays_same_request_without_duplicate_event_or_run(monkeypatch):
    import app.api.session_goals as goals_api
    from app.models.agent_session_goal import AgentSessionGoal

    agent_id = uuid4()
    session_id = uuid4()
    request_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    session = SimpleNamespace(id=session_id)
    goal = AgentSessionGoal(
        id=request_id,
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        created_by_user_id=current_user.id,
        objective="Finish all parity work.",
        status="active",
        metadata_json={"request_id": str(request_id), "last_goal_run_id": "run-original"},
    )
    calls = {"event": 0, "run": 0}

    async def fake_authorize(_db, _user, **_kwargs):
        return SimpleNamespace(agent=agent, session=session)

    async def fake_create_or_load(**_kwargs):
        return goal, False

    async def fake_event(**_kwargs):
        calls["event"] += 1

    async def fake_start(**_kwargs):
        calls["run"] += 1
        return {"run_id": "run-duplicate"}

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)
    monkeypatch.setattr(goals_api, "_create_or_load_goal", fake_create_or_load)
    monkeypatch.setattr(goals_api, "_append_goal_transition_event", fake_event)
    monkeypatch.setattr(goals_api, "start_web_chat_run", fake_start)

    result = await goals_api.start_session_goal(
        agent_id=agent_id,
        session_id=session_id,
        body=goals_api.StartGoalIn(
            request_id=request_id,
            objective="Finish all parity work.",
            start_immediately=True,
        ),
        current_user=current_user,
        db=_FakeDB(),
    )

    assert result["id"] == str(request_id)
    assert result["run"] == {"run_id": "run-original", "status": "running", "replayed": True}
    assert calls == {"event": 0, "run": 0}


@pytest.mark.asyncio
async def test_goal_request_id_insert_and_replay_share_one_canonical_goal():
    import app.api.session_goals as goals_api
    from app.models.agent_session_goal import AgentSessionGoal

    request_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    session_id = uuid4()
    body = goals_api.StartGoalIn(
        request_id=request_id,
        objective="Ship the canonical report",
        start_immediately=True,
    )
    goal = AgentSessionGoal(
        id=request_id,
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        chat_session_id=session_id,
        created_by_user_id=user.id,
        objective=body.objective,
        status="active",
        metadata_json={"request_id": str(request_id)},
    )

    created_goal, created = await goals_api._create_or_load_goal(
        db=_ExecuteDB(request_id, goal),
        agent=agent,
        user=user,
        session_id=session_id,
        body=body,
    )
    replayed_goal, replay_created = await goals_api._create_or_load_goal(
        db=_ExecuteDB(None, goal),
        agent=agent,
        user=user,
        session_id=session_id,
        body=body,
    )

    assert created is True
    assert replay_created is False
    assert created_goal is replayed_goal is goal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "initial_status", "expected_status", "expects_cancel", "expects_continue"),
    [
        ("pause", "active", "paused", True, False),
        ("resume", "paused", "active", False, True),
        ("stop", "active", "cancelled", True, False),
    ],
)
async def test_goals_api_transitions_are_semantic_and_recoverable(
    monkeypatch,
    action,
    initial_status,
    expected_status,
    expects_cancel,
    expects_continue,
):
    import app.api.session_goals as goals_api
    from app.models.agent_session_goal import AgentSessionGoal

    agent_id = uuid4()
    session_id = uuid4()
    goal_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    session = SimpleNamespace(id=session_id)
    goal = AgentSessionGoal(
        id=goal_id,
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Ship the report",
        status=initial_status,
        metadata_json={"last_continuation_run_id": str(uuid4())},
    )
    db = _ExecuteDB(goal)
    calls = {"cancel": 0, "continue": 0, "event": 0}

    async def fake_authorize(_db, _user, **_kwargs):
        return SimpleNamespace(agent=agent, session=session)

    async def fake_cancel(**_kwargs):
        calls["cancel"] += 1
        return {"status": "killed"}

    async def fake_continue(**_kwargs):
        calls["continue"] += 1
        return {"ok": True, "run": {"run_id": "run-resumed"}}

    async def fake_event(**_kwargs):
        calls["event"] += 1

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)
    monkeypatch.setattr(goals_api, "submit_live_cancel_input", fake_cancel)
    monkeypatch.setattr(goals_api, "continue_session_goal", fake_continue)
    monkeypatch.setattr(goals_api, "_append_goal_transition_event", fake_event)

    result = await goals_api.transition_goal(
        agent_id=agent_id,
        session_id=session_id,
        goal_id=goal_id,
        body=goals_api.GoalTransitionIn(action=action),
        current_user=current_user,
        db=db,
    )

    assert result["status"] == expected_status
    assert calls["cancel"] == int(expects_cancel)
    assert calls["continue"] == int(expects_continue)
    assert calls["event"] == 1
    assert bool(result.get("continuation")) is expects_continue


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

    async def fake_authorize(_db, _user, **kwargs):
        assert kwargs["agent_id"] == agent_id
        assert kwargs["session_id"] == session_id
        return SimpleNamespace(agent=agent, session=session)

    async def fake_continue_session_goal(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "goal_id": str(goal_id), "run": {"run_id": "run-1"}}

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)
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
    assert db.executes == 1


@pytest.mark.asyncio
async def test_agent_teams_api_creates_container_only(monkeypatch):
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

    monkeypatch.setattr("app.services.agent_team_runtime_service.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.services.agent_team_runtime_service.append_session_event", fake_append_session_event)

    result = await teams_api.create_agent_team(
        agent_id=agent_id,
        body=teams_api.CreateAgentTeamIn(
            parent_session_id=parent_session_id,
            name="research",
        ),
        current_user=current_user,
        db=db,
    )

    assert result["name"] == "research"
    assert result["status"] == "active"
    assert result["transcript_truth"] == "chat_session_t0"
    assert result["members"] == []
    assert result["team_create_semantics"] == "container_only"
    assert result["teammate_creation_tool"] == "spawn_subagent"
    assert result["teammate_creation_args"]["team_name"] == "research"
    assert db.flushes == 1
    assert {type(item).__name__ for item in db.added} == {"AgentTeam", "AgentTeamEvent"}


def test_agent_teams_api_rejects_inline_members_at_schema_boundary():
    import pytest
    from pydantic import ValidationError

    import app.api.agent_teams as teams_api

    with pytest.raises(ValidationError):
        teams_api.CreateAgentTeamIn(
            parent_session_id=uuid4(),
            name="research",
            members=[{"name": "critic"}],
        )


@pytest.mark.asyncio
async def test_agent_teams_api_lists_enters_and_requests_lead_synthesis(monkeypatch):
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

    notification_id = uuid4()
    notifications = []

    async def fake_enqueue(_db, notification):
        notifications.append(notification)
        return notification_id

    monkeypatch.setattr(teams_api, "check_agent_access", fake_access)
    monkeypatch.setattr(teams_api, "enqueue_completion_notification", fake_enqueue)

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
    assert closed["status"] == "closing"
    assert closed["close_delivery"]["status"] == "pending_lead_synthesis"
    assert team.status == "closing"
    assert member.status == "idle"
    assert closed["consolidation_plan"]["merge_mode"] == "summary_with_t0_refs"
    assert closed["consolidation_plan"]["member_summaries"][0]["t0_refs"] == ["t0://critic/1"]
    assert team.metadata_json["close_summary_ref"] == f"agent_team_close:{team_id}:1"
    assert team.metadata_json["close_notification_id"] == str(notification_id)
    assert len(notifications) == 1
    assert notifications[0].source_kind == "agent_team"
    assert notifications[0].parent_session_id == parent_session_id


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


@pytest.mark.asyncio
async def test_schedule_command_with_only_instruction_hands_drafting_to_agent(monkeypatch):
    """Composer `/schedule <natural language>` must not 400 on missing
    cron_expr — structuring the schedule is the agent's job (AI-Native L1),
    so the command degrades to a chat_prompt that starts a drafting turn."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.api import commands as commands_api

    agent = SimpleNamespace(id=uuid4(), tenant_id=None, creator_id=uuid4())
    user = SimpleNamespace(id=agent.creator_id, role="user")

    result = await commands_api._execute_schedule_command(
        db=None,
        agent=agent,
        user=user,
        command_name="schedule_create",
        session_id=None,
        arguments={"instruction": "每天早上九点给我发昨日日报"},
        access_level="manage",
    )

    assert result["action"] == "chat_prompt"
    assert "每天早上九点" in result["content"]
    assert result["display_content"].startswith("/schedule ")
