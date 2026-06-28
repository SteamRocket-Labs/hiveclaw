from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from datetime import datetime, timezone as tz
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.chat_sessions as chat_sessions_api
from app.core.security import get_current_user
from app.database import get_db


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: [self._value] if self._value else [])


class _FakeDB:
    def __init__(self, session):
        self.session = session

    async def execute(self, _stmt):
        return _ScalarResult(self.session)

    async def commit(self):
        return None

    async def flush(self):
        return None


class _UpdatePermissionModeDB:
    def __init__(self, session, active_run):
        self.session = session
        self.active_run = active_run
        self.calls = 0
        self.commits = 0

    async def execute(self, _stmt):
        self.calls += 1
        return _ScalarResult(self.session if self.calls == 1 else self.active_run)

    async def commit(self):
        self.commits += 1


class _ScalarsResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _ExecuteScalars:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _ScalarsResult(self._values)


class _PermissionDB:
    def __init__(self, events):
        self.events = events
        self.commits = 0

    async def execute(self, _stmt):
        return _ExecuteScalars(self.events)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


class _FilteringPermissionDB(_PermissionDB):
    async def execute(self, stmt):
        try:
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            compiled = str(stmt)
        if "chat_transcript_events.event_type = 'tool_result'" in compiled:
            return _ExecuteScalars(
                [event for event in self.events if getattr(event, "event_type", None) == "tool_result"]
            )
        return _ExecuteScalars(self.events)


def _client(monkeypatch, *, db, user, agent, access_level="use", raise_server_exceptions=True):
    app = FastAPI()
    app.include_router(chat_sessions_api.router)

    async def override_user():
        return user

    async def override_db():
        yield db

    async def allow_access(_db, _user, agent_id):
        assert agent_id == agent.id
        return agent, access_level

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(chat_sessions_api, "check_agent_access", allow_access)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_start_session_run_routes_to_runtime_service(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs",
        json={"content": "hello", "display_content": "hello", "file_name": ""},
    )

    assert response.status_code == 201
    assert response.json() == {"run_id": "run-1", "status": "running"}
    assert captured["db"] is db
    assert captured["agent"] is agent
    assert captured["user"] is user
    assert captured["session"] is session
    assert captured["content"] == "hello"
    assert captured["extra_metadata"] == {
        "permission_mode": "auto",
        "writable_roots": ["workspace/"],
        "permission_profile": {"mode": "auto", "allowed_tools": [], "writable_roots": ["workspace/"]},
    }


def test_start_session_run_threads_ccplus_permission_profile(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        transcript_metadata_json={"session_permission_allowed_tools": ["send_email"]},
    )
    db = _FakeDB(session)
    captured = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs",
        json={"content": "hello", "permission_mode": "default"},
    )

    assert response.status_code == 201
    assert captured["extra_metadata"] == {
        "permission_mode": "default",
        "writable_roots": ["workspace/"],
        "permission_profile": {"mode": "default", "allowed_tools": ["send_email"], "writable_roots": ["workspace/"]},
    }


def test_update_session_permission_mode_updates_session_active_run_and_runtime_context(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        transcript_metadata_json={"permission_mode": "default", "session_permission_allowed_tools": ["track_todo"]},
    )
    active_run = SimpleNamespace(id=uuid4(), metadata_json={"permission_mode": "default"})
    db = _UpdatePermissionModeDB(session, active_run)
    runtime_context = SimpleNamespace(metadata={})

    class _FakeBroker:
        async def get_or_create_runtime_session(self, agent_id_arg, session_id_arg):
            assert agent_id_arg == str(agent_id)
            assert session_id_arg == str(session_id)
            return runtime_context

    monkeypatch.setattr(chat_sessions_api, "web_chat_broker", _FakeBroker())
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.patch(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/profile",
        json={"permission_mode": "bypassPermissions"},
    )

    assert response.status_code == 200
    assert response.json()["permission_mode"] == "bypassPermissions"
    assert session.transcript_metadata_json["permission_mode"] == "bypassPermissions"
    assert session.transcript_metadata_json["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["track_todo"],
        "writable_roots": ["workspace/"],
    }
    assert session.transcript_metadata_json["writable_roots"] == ["workspace/"]
    assert active_run.metadata_json["permission_mode"] == "bypassPermissions"
    assert active_run.metadata_json["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["track_todo"],
        "writable_roots": ["workspace/"],
    }
    assert active_run.metadata_json["writable_roots"] == ["workspace/"]
    assert runtime_context.metadata["permission_mode"] == "bypassPermissions"
    assert runtime_context.metadata["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["track_todo"],
        "writable_roots": ["workspace/"],
    }
    assert runtime_context.metadata["writable_roots"] == ["workspace/"]
    assert db.commits == 1


def test_start_session_run_rejects_non_owner_without_manage_access(monkeypatch):
    agent_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=uuid4())
    db = _FakeDB(session)
    client = _client(monkeypatch, db=db, user=user, agent=agent, access_level="use")

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs",
        json={"content": "hello"},
    )

    assert response.status_code == 403


def test_active_session_run_endpoint_returns_runtime_payload(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)

    async def fake_active(**kwargs):
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "get_active_web_chat_run", fake_active)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.get(f"/agents/{agent_id}/sessions/{session_id}/runs/active")

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-1", "status": "running"}


def test_cancel_session_run_routes_to_runtime_service(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_cancel(**kwargs):
        captured.update(kwargs)
        return {"run_id": run_id.hex, "status": "killed"}

    monkeypatch.setattr(chat_sessions_api, "cancel_web_chat_run", fake_cancel)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(f"/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "killed"
    assert captured["run_id"] == run_id
    assert captured["user_id"] == user_id


def test_resolve_session_permission_denial_emits_permission_denied_hook(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    permission_request_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "send_email",
        "arguments": {"to": "a@example.com"},
        "permission_mode": "auto",
    }
    event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        content=json.dumps(
            {
                "tool_call_id": "tool-call-1",
                "result": json.dumps({"permission_request": permission_request}),
            }
        ),
        metadata_json={"permission_request": permission_request},
    )
    db = _PermissionDB([event])
    emitted_hooks = []
    appended_events = []
    broadcasts = []
    started_runs = []

    async def fake_get_run_session_and_agent(**_kwargs):
        return session, agent, "use"

    async def fake_emit_hook(event_name, **kwargs):
        emitted_hooks.append((event_name, kwargs))

    async def fake_append_session_event(**kwargs):
        appended_events.append(kwargs)

    async def fake_broadcast(*args):
        broadcasts.append(args)

    async def fake_get_active_web_chat_run(**_kwargs):
        return None

    async def fake_start_web_chat_run(**kwargs):
        started_runs.append(kwargs)
        return {"run_id": run_id.hex, "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "emit_hook", fake_emit_hook)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(chat_sessions_api, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(chat_sessions_api, "get_active_web_chat_run", fake_get_active_web_chat_run)
    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start_web_chat_run)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve",
        json={"action": "deny", "feedback": "not now"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "denied"
    assert response.json()["run"] == {"run_id": run_id.hex, "status": "running"}
    assert db.commits == 1
    assert appended_events[-1]["event_type"] == "session_permission_decision"
    assert emitted_hooks
    assert emitted_hooks[-1][0] == chat_sessions_api.HookEvent.PERMISSION_DENIED
    assert emitted_hooks[-1][1]["tool_name"] == "send_email"
    assert emitted_hooks[-1][1]["metadata"]["permission_request"]["permission_request_id"] == str(permission_request_id)
    assert started_runs[0]["append_user_message"] is False
    assert started_runs[0]["extra_metadata"]["source"] == "session_permission_denied_resume"
    assert started_runs[0]["extra_metadata"]["latest_user_prompt_overrides_history"] is True
    assert "denied" in started_runs[0]["content"]
    assert broadcasts[-1][2]["status"] == "denied"
    assert broadcasts[-1][2]["run"] == {"run_id": run_id.hex, "status": "running"}


def test_resolve_session_permission_finds_session_native_permission_event(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    permission_request_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "write_file",
        "arguments": {"path": "workspace/plan.md", "content": "# Plan"},
        "capability": "workspace.file.write",
        "permission_mode": "default",
        "pending_tool_frame": {
            "permission_request_id": str(permission_request_id),
            "session_id": str(session_id),
            "tool_call_id": "tool-call-im",
            "tool_name": "write_file",
            "arguments": {"path": "workspace/plan.md", "content": "# Plan"},
            "origin_channel": "feishu",
            "runtime_task_id": "runtime-im",
            "turn_id": "turn-im",
            "round_state": {"round": 2},
            "t0_refs": ["t0://sessions/session-native/events/9"],
        },
    }
    event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        event_type="permission",
        content=json.dumps(
            {
                "type": "permission",
                "status": "session_permission_required",
                "permission_request_id": str(permission_request_id),
                "permission_request": permission_request,
                "tool_name": "write_file",
            }
        ),
        metadata_json={
            "runtime_event_type": "permission",
            "permission_request_id": str(permission_request_id),
            "permission_request": permission_request,
        },
    )
    db = _FilteringPermissionDB([event])
    emitted_hooks = []
    appended_events = []
    broadcasts = []
    started_runs = []

    async def fake_get_run_session_and_agent(**_kwargs):
        return session, agent, "use"

    async def fake_emit_hook(event_name, **kwargs):
        emitted_hooks.append((event_name, kwargs))

    async def fake_append_session_event(**kwargs):
        appended_events.append(kwargs)

    async def fake_broadcast(*args):
        broadcasts.append(args)

    async def fake_get_active_web_chat_run(**_kwargs):
        return None

    async def fake_start_web_chat_run(**kwargs):
        started_runs.append(kwargs)
        return {"run_id": run_id.hex, "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "emit_hook", fake_emit_hook)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(chat_sessions_api, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(chat_sessions_api, "get_active_web_chat_run", fake_get_active_web_chat_run)
    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start_web_chat_run)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve",
        json={"action": "deny", "feedback": "not now"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "denied"
    assert response.json()["run"] == {"run_id": run_id.hex, "status": "running"}
    assert appended_events[-1]["metadata"]["source_event_id"] == str(event.id)
    assert emitted_hooks[-1][1]["tool_name"] == "write_file"
    assert started_runs[0]["append_user_message"] is False
    assert started_runs[0]["extra_metadata"]["source"] == "session_permission_denied_resume"
    assert started_runs[0]["extra_metadata"]["origin_channel"] == "feishu"
    assert started_runs[0]["extra_metadata"]["resumed_runtime_task_id"] == "runtime-im"
    assert started_runs[0]["extra_metadata"]["resumed_turn_id"] == "turn-im"
    assert started_runs[0]["extra_metadata"]["round_state"] == {"round": 2}
    assert started_runs[0]["extra_metadata"]["t0_refs"] == ["t0://sessions/session-native/events/9"]
    assert started_runs[0]["extra_metadata"]["resumed_from_permission_request_id"] == str(permission_request_id)
    assert broadcasts[-1][2]["status"] == "denied"
    assert broadcasts[-1][2]["run"] == {"run_id": run_id.hex, "status": "running"}


def test_resolve_session_permission_allow_records_checkpoint_and_replays_original_tool_call_id(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    permission_request_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    arguments = {"path": "workspace/plan.md", "content": "# Plan"}
    pending_frame = {
        "permission_request_id": str(permission_request_id),
        "session_id": str(session_id),
        "tool_call_id": "tool-call-checkpoint",
        "tool_name": "write_file",
        "arguments": arguments,
        "origin_channel": "feishu",
        "runtime_task_id": "runtime-im",
        "turn_id": "turn-im",
        "round_state": {"round": 2},
        "t0_refs": ["t0://sessions/session-native/events/9"],
        "permission_profile": {
            "mode": "default",
            "allowed_tools": [],
            "writable_roots": ["workspace/"],
        },
        "created_at": "2026-06-28T00:00:00+00:00",
        "expires_at": "2099-06-28T00:30:00+00:00",
        "status": "pending",
    }
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "write_file",
        "arguments": arguments,
        "capability": "workspace.file.write",
        "permission_mode": "default",
        "pending_tool_frame": pending_frame,
    }
    event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        event_type="permission",
        content=json.dumps(
            {
                "type": "permission",
                "status": "session_permission_required",
                "permission_request_id": str(permission_request_id),
                "permission_request": permission_request,
                "tool_name": "write_file",
            }
        ),
        metadata_json={
            "runtime_event_type": "permission",
            "permission_request_id": str(permission_request_id),
            "permission_request": permission_request,
        },
    )
    db = _FilteringPermissionDB([event])
    appended_events = []
    broadcasts = []
    persisted_calls = []
    captured_execute = {}
    started_runs = []

    async def fake_get_run_session_and_agent(**_kwargs):
        return session, agent, "use"

    async def fake_append_session_event(**kwargs):
        appended_events.append(kwargs)

    async def fake_broadcast(*args):
        broadcasts.append(args)

    async def fake_execute_session_permission_tool(*args, **kwargs):
        captured_execute["args"] = args
        captured_execute["kwargs"] = kwargs
        return "write complete"

    async def fake_persist_tool_call(**kwargs):
        persisted_calls.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=101, transcript_event=SimpleNamespace(metadata_json={}))

    async def fake_get_active_web_chat_run(**_kwargs):
        return None

    async def fake_start_web_chat_run(**kwargs):
        started_runs.append(kwargs)
        return {"run_id": run_id.hex, "status": "running"}

    import app.services.agent_tools as agent_tools_service

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(chat_sessions_api, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(chat_sessions_api, "_persist_tool_call", fake_persist_tool_call)
    monkeypatch.setattr(chat_sessions_api, "get_active_web_chat_run", fake_get_active_web_chat_run)
    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start_web_chat_run)
    monkeypatch.setattr(agent_tools_service, "execute_session_permission_tool", fake_execute_session_permission_tool)
    client = _client(monkeypatch, db=db, user=user, agent=agent, raise_server_exceptions=False)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve",
        json={"action": "allow_once"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "allowed"
    decision_metadata = appended_events[0]["metadata"]
    checkpoint = decision_metadata["permission_checkpoint"]
    assert checkpoint["permission_request_id"] == str(permission_request_id)
    assert checkpoint["decision"] == "allow_once"
    assert checkpoint["pending_frame"]["tool_call_id"] == "tool-call-checkpoint"
    assert checkpoint["pending_frame"]["permission_profile"]["mode"] == "default"
    assert decision_metadata["tool_call_id"] == "tool-call-checkpoint"
    assert captured_execute["kwargs"]["tool_call_id"] == "tool-call-checkpoint"
    assert captured_execute["kwargs"]["origin_channel"] == "feishu"
    assert persisted_calls[-1]["data"]["tool_call_id"] == "tool-call-checkpoint"
    assert started_runs[0]["append_user_message"] is False
    assert started_runs[0]["extra_metadata"]["source"] == "session_permission_resume"
    assert started_runs[0]["extra_metadata"]["origin_channel"] == "feishu"
    assert started_runs[0]["extra_metadata"]["channel"] == "feishu"
    assert started_runs[0]["extra_metadata"]["resumed_runtime_task_id"] == "runtime-im"
    assert started_runs[0]["extra_metadata"]["resumed_turn_id"] == "turn-im"
    assert started_runs[0]["extra_metadata"]["round_state"] == {"round": 2}
    assert started_runs[0]["extra_metadata"]["t0_refs"] == ["t0://sessions/session-native/events/9"]
    assert broadcasts[-1][2]["permission_checkpoint"]["pending_frame"]["tool_call_id"] == "tool-call-checkpoint"


def test_resolve_session_permission_rejects_duplicate_resolution_before_reexecution(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    permission_request_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "write_file",
        "arguments": {"path": "workspace/plan.md", "content": "# Plan"},
        "capability": "workspace.file.write",
        "permission_mode": "default",
    }
    already_resolved_event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        event_type="session_permission_decision",
        content=json.dumps(
            {
                "event_type": "session_permission_decision",
                "permission_request_id": str(permission_request_id),
                "permission_request": permission_request,
                "decision": "allow_once",
                "status": "allowed",
            }
        ),
        metadata_json={
            "permission_request_id": str(permission_request_id),
            "permission_request": permission_request,
            "decision": "allow_once",
            "status": "allowed",
        },
    )
    pending_event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        event_type="permission",
        content=json.dumps(
            {
                "type": "permission",
                "status": "session_permission_required",
                "permission_request_id": str(permission_request_id),
                "permission_request": permission_request,
                "tool_name": "write_file",
            }
        ),
        metadata_json={
            "runtime_event_type": "permission",
            "permission_request_id": str(permission_request_id),
            "permission_request": permission_request,
        },
    )
    db = _FilteringPermissionDB([already_resolved_event, pending_event])

    async def fake_get_run_session_and_agent(**_kwargs):
        return session, agent, "use"

    async def fake_append_session_event(**_kwargs):
        raise AssertionError("duplicate resolve must not append another decision event")

    async def fake_execute_session_permission_tool(*_args, **_kwargs):
        raise AssertionError("duplicate resolve must not execute the tool again")

    import app.services.agent_tools as agent_tools_service

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(agent_tools_service, "execute_session_permission_tool", fake_execute_session_permission_tool)
    client = _client(monkeypatch, db=db, user=user, agent=agent, raise_server_exceptions=False)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve",
        json={"action": "allow_once"},
    )

    assert response.status_code == 409
    assert "already resolved" in response.text
    assert db.commits == 0


def test_resolve_session_permission_rejects_expired_request_before_execution(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    permission_request_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "write_file",
        "arguments": {"path": "workspace/plan.md", "content": "# Plan"},
        "capability": "workspace.file.write",
        "permission_mode": "default",
        "pending_tool_frame": {
            "permission_request_id": str(permission_request_id),
            "session_id": str(session_id),
            "tool_call_id": "tool-call-expired",
            "tool_name": "write_file",
            "arguments": {"path": "workspace/plan.md", "content": "# Plan"},
            "expires_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        },
    }
    event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        event_type="permission",
        content=json.dumps(
            {
                "type": "permission",
                "status": "session_permission_required",
                "permission_request_id": str(permission_request_id),
                "permission_request": permission_request,
                "tool_name": "write_file",
            }
        ),
        metadata_json={
            "runtime_event_type": "permission",
            "permission_request_id": str(permission_request_id),
            "permission_request": permission_request,
        },
    )
    db = _FilteringPermissionDB([event])
    appended_events = []

    async def fake_get_run_session_and_agent(**_kwargs):
        return session, agent, "use"

    async def fake_append_session_event(**kwargs):
        appended_events.append(kwargs)

    async def fake_execute_session_permission_tool(*_args, **_kwargs):
        raise AssertionError("expired permission request must not execute the tool")

    import app.services.agent_tools as agent_tools_service

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(agent_tools_service, "execute_session_permission_tool", fake_execute_session_permission_tool)
    client = _client(monkeypatch, db=db, user=user, agent=agent, raise_server_exceptions=False)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve",
        json={"action": "allow_once"},
    )

    assert response.status_code == 410
    assert "expired" in response.text
    assert appended_events[-1]["event_type"] == "session_permission_expired"
    assert appended_events[-1]["metadata"]["permission_request_id"] == str(permission_request_id)
    assert db.commits == 1


def test_expire_stale_session_permission_requests_marks_pending_expired(monkeypatch):
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    permission_request_id = uuid4()
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "write_file",
        "arguments": {"path": "workspace/plan.md", "content": "# Plan"},
        "pending_tool_frame": {
            "permission_request_id": str(permission_request_id),
            "session_id": str(session_id),
            "tool_name": "write_file",
            "expires_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        },
    }
    pending_event = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        event_type="permission",
        content=json.dumps({"permission_request": permission_request}),
        metadata_json={"permission_request_id": str(permission_request_id), "permission_request": permission_request},
    )
    db = _FilteringPermissionDB([pending_event])
    appended_events = []

    async def fake_append_session_event(**kwargs):
        appended_events.append(kwargs)

    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)

    expired_count = asyncio.run(
        chat_sessions_api.expire_stale_session_permission_requests(
            db=db,
            now=datetime(2026, 6, 28, tzinfo=tz.utc),
        )
    )

    assert expired_count == 1
    assert appended_events[-1]["event_type"] == "session_permission_expired"
    assert appended_events[-1]["agent_id"] == agent_id
    assert appended_events[-1]["session_id"] == session_id
    assert appended_events[-1]["metadata"]["permission_request_id"] == str(permission_request_id)
    assert db.commits == 1


def test_resolve_session_permission_rejects_allow_session_for_destructive_request(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    permission_request_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "run_command",
        "arguments": {"command": "rm workspace/report.md"},
        "capability": "workspace.command.destructive_delete",
        "permission_mode": "bypassPermissions",
        "risk_class": "destructive_delete",
        "confirmation_kind": "destructive_once",
        "allow_session_allowed": False,
    }
    event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        event_type="permission",
        content=json.dumps(
            {
                "type": "permission",
                "status": "session_permission_required",
                "permission_request_id": str(permission_request_id),
                "permission_request": permission_request,
                "tool_name": "run_command",
            }
        ),
        metadata_json={
            "runtime_event_type": "permission",
            "permission_request_id": str(permission_request_id),
            "permission_request": permission_request,
        },
    )
    db = _FilteringPermissionDB([event])

    async def fake_get_run_session_and_agent(**_kwargs):
        return session, agent, "use"

    async def fake_append_session_event(**_kwargs):
        raise AssertionError("rejected allow_session must not append a decision event")

    async def fake_execute_session_permission_tool(*_args, **_kwargs):
        raise AssertionError("rejected allow_session must not execute the tool")

    import app.services.agent_tools as agent_tools_service

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(agent_tools_service, "execute_session_permission_tool", fake_execute_session_permission_tool)
    client = _client(monkeypatch, db=db, user=user, agent=agent, raise_server_exceptions=False)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve",
        json={"action": "allow_session"},
    )

    assert response.status_code == 400
    assert "only be allowed once" in response.text
    assert session.transcript_metadata_json == {}


def test_resolve_session_permission_allow_failure_records_session_event_instead_of_500(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    permission_request_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "write_file",
        "arguments": {"path": "workspace/plan.md", "content": "# Plan"},
        "capability": "workspace.file.write",
        "permission_mode": "default",
    }
    event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        event_type="permission",
        content=json.dumps(
            {
                "type": "permission",
                "status": "session_permission_required",
                "permission_request_id": str(permission_request_id),
                "permission_request": permission_request,
                "tool_name": "write_file",
            }
        ),
        metadata_json={
            "runtime_event_type": "permission",
            "permission_request_id": str(permission_request_id),
            "permission_request": permission_request,
        },
    )
    db = _FilteringPermissionDB([event])
    appended_events = []
    broadcasts = []

    async def fake_get_run_session_and_agent(**_kwargs):
        return session, agent, "use"

    async def fake_append_session_event(**kwargs):
        appended_events.append(kwargs)

    async def fake_broadcast(*args):
        broadcasts.append(args)

    async def fake_execute_session_permission_tool(*_args, **_kwargs):
        raise RuntimeError("workspace write still blocked")

    import app.services.agent_tools as agent_tools_service

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(chat_sessions_api, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(agent_tools_service, "execute_session_permission_tool", fake_execute_session_permission_tool)
    client = _client(monkeypatch, db=db, user=user, agent=agent, raise_server_exceptions=False)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve",
        json={"action": "allow_once"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["permission_request_id"] == str(permission_request_id)
    assert response.json()["error"] == "workspace write still blocked"
    assert appended_events[-1]["event_type"] == "permission_resolved"
    assert appended_events[-1]["metadata"]["tool_name"] == "write_file"
    assert appended_events[-1]["metadata"]["error"] == "workspace write still blocked"
    assert appended_events[-1]["metadata"]["status"] == "failed"
    assert broadcasts[-1][2]["status"] == "failed"
    assert broadcasts[-1][2]["error"] == "workspace write still blocked"


def test_resolve_session_permission_allow_reuses_active_run_instead_of_starting_new_one(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    permission_request_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    permission_request = {
        "permission_request_id": str(permission_request_id),
        "tool_name": "write_file",
        "arguments": {"path": "workspace/plan.md", "content": "# Plan"},
        "capability": "workspace.file.write",
        "permission_mode": "default",
    }
    event = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        event_type="permission",
        content=json.dumps(
            {
                "type": "permission",
                "status": "session_permission_required",
                "permission_request_id": str(permission_request_id),
                "permission_request": permission_request,
                "tool_name": "write_file",
            }
        ),
        metadata_json={
            "runtime_event_type": "permission",
            "permission_request_id": str(permission_request_id),
            "permission_request": permission_request,
        },
    )
    db = _FilteringPermissionDB([event])
    broadcasts = []
    persisted_calls = []

    async def fake_get_run_session_and_agent(**_kwargs):
        return session, agent, "use"

    async def fake_append_session_event(**_kwargs):
        return None

    async def fake_broadcast(*args):
        broadcasts.append(args)

    async def fake_execute_session_permission_tool(*_args, **_kwargs):
        return "write complete"

    async def fake_persist_tool_call(**kwargs):
        persisted_calls.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=101, transcript_event=SimpleNamespace(metadata_json={}))

    async def fake_get_active_web_chat_run(**_kwargs):
        return {"run_id": run_id.hex, "status": "running"}

    async def fake_start_web_chat_run(**_kwargs):
        raise AssertionError("should not start a new run while an active turn exists")

    import app.services.agent_tools as agent_tools_service

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_run_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(chat_sessions_api, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(chat_sessions_api, "_persist_tool_call", fake_persist_tool_call)
    monkeypatch.setattr(chat_sessions_api, "get_active_web_chat_run", fake_get_active_web_chat_run)
    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start_web_chat_run)
    monkeypatch.setattr(agent_tools_service, "execute_session_permission_tool", fake_execute_session_permission_tool)
    client = _client(monkeypatch, db=db, user=user, agent=agent, raise_server_exceptions=False)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve",
        json={"action": "allow_once"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "allowed"
    assert response.json()["run"] == {"run_id": run_id.hex, "status": "running"}
    assert persisted_calls[-1]["data"]["result"] == "write complete"
    assert broadcasts[-1][2]["status"] == "allowed"


def test_steer_session_turn_routes_to_runtime_service(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_steer(**kwargs):
        captured.update(kwargs)
        return {
            "run_id": "run-1",
            "turn_id": kwargs["expected_turn_id"],
            "queued": {"content": kwargs["content"]},
            "steer_strategy": "pending_mid_run_user_message",
        }

    monkeypatch.setattr(chat_sessions_api, "steer_active_web_chat_turn", fake_steer)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/turns/steer",
        json={"content": "Narrow the answer.", "expected_turn_id": "turn-1"},
    )

    assert response.status_code == 200
    assert response.json()["steer_strategy"] == "pending_mid_run_user_message"
    assert captured["session"] is session
    assert captured["content"] == "Narrow the answer."
    assert captured["expected_turn_id"] == "turn-1"


def test_thread_turn_interrupt_alias_routes_to_runtime_service(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_cancel(**kwargs):
        captured.update(kwargs)
        return {"run_id": str(kwargs["run_id"]), "status": "killed"}

    monkeypatch.setattr(chat_sessions_api, "cancel_web_chat_run", fake_cancel)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(f"/agents/{agent_id}/threads/{session_id}/turns/{run_id}/interrupt")

    assert response.status_code == 200
    assert response.json()["status"] == "killed"
    assert captured["run_id"] == run_id
    assert captured["user_id"] == user_id


def test_thread_read_alias_returns_json_export(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)

    async def fake_export(_db, **kwargs):
        return {"session": {"id": str(kwargs["session"].id)}, "transcript_events": []}

    monkeypatch.setattr(chat_sessions_api, "build_session_json_export", fake_export)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.get(f"/agents/{agent_id}/threads/{session_id}/read")

    assert response.status_code == 200
    assert response.json()["session"]["id"] == str(session_id)
