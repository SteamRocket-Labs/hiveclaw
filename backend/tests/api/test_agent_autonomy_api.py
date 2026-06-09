from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.autonomy as autonomy_api
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    pass


class _ScalarOneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _SessionDB:
    def __init__(self, session):
        self.session = session
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarOneResult(self.session)


def _client(monkeypatch, db=None, *, user=None, access_level="manage", agent=None):
    app = FastAPI()
    app.include_router(autonomy_api.router)
    user = user or SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")

    async def override_user():
        return user

    async def override_db():
        yield db or _FakeDB()

    async def allow_access(_db, _user, agent_id):
        return (
            agent
            or SimpleNamespace(id=agent_id, tenant_id=_user.tenant_id, creator_id=uuid4()),
            access_level,
        )

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(autonomy_api, "check_agent_access", allow_access)
    return TestClient(app), user


def test_agent_autonomy_overview_is_agent_scoped_and_readable_by_member(monkeypatch):
    agent_id = uuid4()
    captured = {}

    async def fake_overview(*, db, agent, lookback_hours, include_diagnostics):
        captured["agent"] = agent
        captured["lookback_hours"] = lookback_hours
        captured["include_diagnostics"] = include_diagnostics
        return {
            "agent_id": str(agent.id),
            "lookback_hours": lookback_hours,
            "totals": {"objectives": 1, "triggers": 1, "recent_attempts": 0, "findings": 0},
            "objectives": [{"id": "objective-1", "description": "Send report", "status": "active"}],
            "triggers": [{"id": "trigger-1", "display_kind": "objective_task", "attention_state": "active"}],
            "recent_attempts": [],
            "findings": [],
        }

    monkeypatch.setattr(autonomy_api, "build_agent_autonomy_overview", fake_overview)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/autonomy/overview", params={"lookback_hours": 6})

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_id"] == str(agent_id)
    assert payload["triggers"][0]["display_kind"] == "objective_task"
    assert captured["lookback_hours"] == 6
    assert captured["include_diagnostics"] is False


def test_agent_autonomy_diagnostics_explicitly_includes_diagnostics(monkeypatch):
    agent_id = uuid4()
    captured = {}

    async def fake_overview(*, db, agent, lookback_hours, include_diagnostics):
        captured["include_diagnostics"] = include_diagnostics
        return {
            "agent_id": str(agent.id),
            "lookback_hours": lookback_hours,
            "totals": {"objectives": 0, "triggers": 1, "recent_attempts": 0, "findings": 0},
            "objectives": [],
            "triggers": [
                {
                    "id": "trigger-1",
                    "display_kind": "scheduled_job",
                    "attention_state": "backoff_active",
                    "diagnostics": {"trigger_class": "scheduled_job", "backoff_until": "2026-04-27T09:00:00Z"},
                }
            ],
            "recent_attempts": [],
            "findings": [],
        }

    monkeypatch.setattr(autonomy_api, "build_agent_autonomy_overview", fake_overview)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/autonomy/diagnostics")

    assert response.status_code == 200
    assert captured["include_diagnostics"] is True
    assert response.json()["triggers"][0]["diagnostics"]["trigger_class"] == "scheduled_job"


def test_agent_runtime_tasks_endpoint_passes_filters(monkeypatch):
    agent_id = uuid4()
    captured = {}

    async def fake_runtime_tasks(*, db, agent_id, task_type, trigger_id, status, limit, include_diagnostics):
        captured.update(
            {
                "agent_id": agent_id,
                "task_type": task_type,
                "trigger_id": trigger_id,
                "status": status,
                "limit": limit,
                "include_diagnostics": include_diagnostics,
            }
        )
        return [{"task_id": "task-1", "status": "skipped", "attention_reason": "No model is configured."}]

    monkeypatch.setattr(autonomy_api, "list_agent_runtime_task_views", fake_runtime_tasks)
    client, _user = _client(monkeypatch)

    response = client.get(
        f"/agents/{agent_id}/runtime-tasks",
        params={"task_type": "trigger", "status": "skipped", "limit": 5, "diagnostics": "true"},
    )

    assert response.status_code == 200
    assert response.json()[0]["status"] == "skipped"
    assert captured["agent_id"] == agent_id
    assert captured["task_type"] == "trigger"
    assert captured["status"] == "skipped"
    assert captured["limit"] == 5
    assert captured["include_diagnostics"] is True


def test_agent_runtime_artifact_endpoint_returns_display_payload(monkeypatch):
    agent_id = uuid4()
    runtime_task_id = uuid4().hex
    captured = {}

    async def fake_artifact(*, agent_id, runtime_task_id, include_diagnostics):
        captured["agent_id"] = agent_id
        captured["runtime_task_id"] = runtime_task_id
        captured["include_diagnostics"] = include_diagnostics
        return {"title": "daily_report", "summary": "Report delivered.", "final_reply": "Report delivered."}

    monkeypatch.setattr(autonomy_api, "read_agent_trigger_artifact_view", fake_artifact)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/runtime-artifacts/{runtime_task_id}")

    assert response.status_code == 200
    assert response.json()["summary"] == "Report delivered."
    assert captured["agent_id"] == agent_id
    assert captured["runtime_task_id"] == runtime_task_id
    assert captured["include_diagnostics"] is False


def test_agent_runtime_work_ledger_endpoint_returns_chat_safe_todolist(monkeypatch):
    agent_id = uuid4()
    runtime_task_id = uuid4().hex
    captured = {}

    def fake_work_ledger(*, agent_id, runtime_task_id):
        captured["agent_id"] = agent_id
        captured["runtime_task_id"] = runtime_task_id
        return {
            "schema": "agent_work_ledger_view.v1",
            "runtime_task_id": runtime_task_id,
            "status": "running",
            "current_phase": "collect_sources",
            "todo_items": [
                {"id": "todo-1", "title": "Collect and grade sources", "status": "running", "required": True},
                {"id": "todo-2", "title": "Write final report", "status": "pending", "required": True},
            ],
            "counts": {"todos_total": 2, "todos_complete": 0, "todos_open": 2, "progress_count": 3},
        }

    monkeypatch.setattr(autonomy_api, "read_agent_work_ledger_view", fake_work_ledger)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/runtime-work-ledgers/{runtime_task_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "agent_work_ledger_view.v1"
    assert payload["current_phase"] == "collect_sources"
    assert payload["todo_items"][0]["title"] == "Collect and grade sources"
    assert captured["agent_id"] == agent_id
    assert captured["runtime_task_id"] == runtime_task_id


def test_agent_runtime_work_ledger_endpoint_404s_when_missing(monkeypatch):
    agent_id = uuid4()
    runtime_task_id = uuid4().hex

    def fake_work_ledger(*, agent_id, runtime_task_id):
        return None

    monkeypatch.setattr(autonomy_api, "read_agent_work_ledger_view", fake_work_ledger)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/runtime-work-ledgers/{runtime_task_id}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Runtime work ledger not found"


def test_agent_session_work_ledger_endpoint_returns_latest_session_ledger(monkeypatch):
    agent_id = uuid4()
    session_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user.id)
    db = _SessionDB(session)
    captured = {}

    async def fake_session_work_ledger(*, db, agent_id, session_id):
        captured["db"] = db
        captured["agent_id"] = agent_id
        captured["session_id"] = session_id
        return {
            "schema": "agent_work_ledger_view.v1",
            "session_id": str(session_id),
            "runtime_task_id": uuid4().hex,
            "status": "running",
            "current_phase": "execute_todos",
            "todo_items": [
                {"id": "todo-1", "title": "Implement requested changes", "status": "running", "required": True},
            ],
            "counts": {"todos_total": 1, "todos_complete": 0, "todos_open": 1, "progress_count": 2},
        }

    monkeypatch.setattr(autonomy_api, "read_latest_session_work_ledger_view", fake_session_work_ledger)
    client, _user = _client(monkeypatch, db=db, user=user, access_level="read")

    response = client.get(f"/agents/{agent_id}/sessions/{session_id}/work-ledger")

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == str(session_id)
    assert payload["todo_items"][0]["title"] == "Implement requested changes"
    assert captured["agent_id"] == agent_id
    assert captured["session_id"] == session_id


def test_agent_session_work_ledger_endpoint_returns_empty_view_before_ledger_exists(monkeypatch):
    agent_id = uuid4()
    session_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user.id)
    db = _SessionDB(session)

    async def fake_session_work_ledger(*, db, agent_id, session_id):
        return None

    monkeypatch.setattr(autonomy_api, "read_latest_session_work_ledger_view", fake_session_work_ledger)
    client, _user = _client(monkeypatch, db=db, user=user, access_level="read")

    response = client.get(f"/agents/{agent_id}/sessions/{session_id}/work-ledger")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "schema": "agent_work_ledger_view.v1",
        "session_id": str(session_id),
        "runtime_task_id": None,
        "status": "empty",
        "current_phase": None,
        "todo_items": [],
        "counts": {"todos_total": 0, "todos_complete": 0, "todos_open": 0},
    }


def test_agent_session_work_ledger_endpoint_rejects_cross_user_session_without_manage(monkeypatch):
    agent_id = uuid4()
    session_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=uuid4())
    db = _SessionDB(session)
    called = False

    async def fake_session_work_ledger(**_kwargs):
        nonlocal called
        called = True
        return {"schema": "agent_work_ledger_view.v1", "todo_items": []}

    monkeypatch.setattr(autonomy_api, "read_latest_session_work_ledger_view", fake_session_work_ledger)
    client, _user = _client(monkeypatch, db=db, user=user, access_level="read")

    response = client.get(f"/agents/{agent_id}/sessions/{session_id}/work-ledger")

    assert response.status_code == 403
    assert response.json()["detail"] == "Not authorized to view this session work ledger"
    assert called is False


def test_agent_session_work_ledger_endpoint_allows_cross_user_session_for_manager(monkeypatch):
    agent_id = uuid4()
    session_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=uuid4())
    db = _SessionDB(session)

    async def fake_session_work_ledger(*, db, agent_id, session_id):
        return {
            "schema": "agent_work_ledger_view.v1",
            "session_id": str(session_id),
            "runtime_task_id": uuid4().hex,
            "status": "running",
            "todo_items": [{"id": "todo-1", "title": "Manager visible todo", "status": "running"}],
        }

    monkeypatch.setattr(autonomy_api, "read_latest_session_work_ledger_view", fake_session_work_ledger)
    client, _user = _client(monkeypatch, db=db, user=user, access_level="manage")

    response = client.get(f"/agents/{agent_id}/sessions/{session_id}/work-ledger")

    assert response.status_code == 200
    assert response.json()["todo_items"][0]["title"] == "Manager visible todo"
