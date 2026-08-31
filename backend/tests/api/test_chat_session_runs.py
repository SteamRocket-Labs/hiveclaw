from __future__ import annotations

import uuid
from types import SimpleNamespace
from uuid import uuid4

import pytest
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
        if "session_tool_invocations" in str(_stmt):
            return _ScalarResult(None)
        return _ScalarResult(self.session)

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def flush(self):
        return None


class _CreateAndRunDB:
    def __init__(self):
        self.added = []
        self.flushes = 0
        self.commits = 0
        self.refreshed = []

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def refresh(self, value):
        self.refreshed.append(value)


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

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        await kwargs["db"].commit()
        return {"schema": "hive.human_input_receipt", "run": {"run_id": "run-1", "status": "running"}}

    monkeypatch.setattr(chat_sessions_api, "submit_live_human_input", fake_submit)
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
    assert captured["source"] == "legacy_rest_start_session_run"
    assert captured["runtime_metadata"] == {
        "model_routing_locked": False,
        "permission_mode": "default",
        "writable_roots": ["workspace/"],
        "permission_profile": {
            "mode": "default",
            "allowed_tools": [],
            "session_grants": [],
            "writable_roots": ["workspace/"],
        },
    }


def test_start_session_run_returns_exact_nonretryable_tool_effect_hold(monkeypatch):
    from app.services.session_tool_runtime import ToolEffectReconciliationRequired

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    held_run_id = uuid4()
    invocation_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)

    async def fail_closed(**_kwargs):
        raise ToolEffectReconciliationRequired(
            session_id=session_id,
            run_ids=(held_run_id,),
            invocation_ids=(invocation_id,),
        )

    monkeypatch.setattr(chat_sessions_api, "submit_live_human_input", fail_closed)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs",
        json={"content": "do not replay the write"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "tool_effect_reconciliation_required",
        "session_id": str(session_id),
        "retryable": False,
    }


def test_start_session_run_rejects_read_only_peer_a2a_session_before_runtime_dispatch(monkeypatch):
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_kind="delegation_run",
        runtime_source="delegation",
    )
    db = _FakeDB(session)

    async def must_not_submit(**_kwargs):
        raise AssertionError("read-only peer A2A Session must fail before runtime dispatch")

    monkeypatch.setattr(chat_sessions_api, "submit_live_human_input", must_not_submit)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs",
        json={"content": "try to take over the peer employee", "display_content": "try", "file_name": ""},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "session_read_only",
        "session_kind": "delegation_run",
        "action": "start_session_run",
    }


def test_create_session_run_atomically_creates_human_session_and_starts_runtime(monkeypatch):
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=uuid4(),
        tenant_id=tenant_id,
        default_session_permission_mode="auto",
    )
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    db = _CreateAndRunDB()
    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        await kwargs["db"].commit()
        return {"schema": "hive.human_input_receipt", "run": {"run_id": "run-1", "status": "running"}}

    monkeypatch.setattr(chat_sessions_api, "submit_live_human_input", fake_submit)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/runs",
        json={
            "title": "Session 07-01 03:16",
            "content": "hello",
            "display_content": "hello",
            "file_name": "",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["run"] == {"run_id": "run-1", "status": "running"}
    assert payload["session"]["id"] == str(captured["session"].id)
    assert payload["session"]["agent_id"] == str(agent_id)
    assert payload["session"]["user_id"] == str(user_id)
    assert payload["session"]["title"] == "Session 07-01 03:16"
    assert payload["session"]["source_channel"] == "web"
    assert payload["session"]["session_kind"] == "human_chat"
    assert payload["session"]["actor_type"] == "user"
    assert payload["session"]["runtime_source"] == "web_chat"
    assert payload["session"]["visibility_scope"] == "direct_user"
    assert payload["session"]["listed_surface"] == "chat"
    assert payload["session"]["is_current_user_session"] is True
    assert payload["session"]["read_only"] is False
    assert db.added == [captured["session"]]
    assert db.flushes == 1
    assert db.commits == 1
    assert db.refreshed == [captured["session"]]
    assert captured["db"] is db
    assert captured["agent"] is agent
    assert captured["user"] is user
    assert captured["content"] == "hello"
    assert captured["display_content"] == "hello"
    assert captured["file_name"] == ""
    assert captured["source"] == "legacy_rest_create_session_run"
    assert captured["runtime_metadata"] == {
        "model_routing_locked": False,
        "permission_mode": "auto",
        "writable_roots": ["workspace/"],
        "permission_profile": {
            "mode": "auto",
            "allowed_tools": [],
            "session_grants": [],
            "writable_roots": ["workspace/"],
        },
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

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return {"schema": "hive.human_input_receipt", "run": {"run_id": "run-1", "status": "running"}}

    monkeypatch.setattr(chat_sessions_api, "submit_live_human_input", fake_submit)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs",
        json={"content": "hello", "permission_mode": "default"},
    )

    assert response.status_code == 201
    assert captured["runtime_metadata"] == {
        "model_routing_locked": False,
        "permission_mode": "default",
        "writable_roots": ["workspace/"],
        "permission_profile": {
            "mode": "default",
            "allowed_tools": ["send_email"],
            "session_grants": [],
            "writable_roots": ["workspace/"],
        },
    }


def test_update_session_permission_mode_updates_session_active_run_and_runtime_context(monkeypatch):
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
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

    async def fake_append_session_event(**_kwargs):
        return None

    async def fake_broadcast_web_chat_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_sessions_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(chat_sessions_api, "broadcast_web_chat_event", fake_broadcast_web_chat_event)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.patch(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/profile",
        json={
            "permission_mode": "bypassPermissions",
        },
    )

    assert response.status_code == 200
    assert response.json()["permission_mode"] == "bypassPermissions"
    assert session.transcript_metadata_json["permission_mode"] == "bypassPermissions"
    assert session.transcript_metadata_json["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["track_todo"],
        "session_grants": [],
        "writable_roots": ["workspace/"],
    }
    assert session.transcript_metadata_json["writable_roots"] == ["workspace/"]
    assert active_run.metadata_json["permission_mode"] == "bypassPermissions"
    assert active_run.metadata_json["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["track_todo"],
        "session_grants": [],
        "writable_roots": ["workspace/"],
    }
    assert active_run.metadata_json["writable_roots"] == ["workspace/"]
    assert runtime_context.metadata["permission_mode"] == "bypassPermissions"
    assert runtime_context.metadata["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["track_todo"],
        "session_grants": [],
        "writable_roots": ["workspace/"],
    }
    assert runtime_context.metadata["writable_roots"] == ["workspace/"]
    assert "break_glass" not in runtime_context.metadata
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


def test_cancel_session_run_routes_to_typed_control_input_service(monkeypatch):
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_cancel(**kwargs):
        captured.update(kwargs)
        return {"control_id": "control-1", "status": "applying"}

    monkeypatch.setattr(chat_sessions_api, "_cancel_session_run_v2", fake_cancel)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(f"/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "applying"
    assert captured["agent_id"] == agent_id
    assert captured["run_id"] == run_id
    assert captured["current_user"] is user


def _install_native_permission_endpoint_fakes(
    monkeypatch,
    *,
    session,
    agent,
    authority,
    resolver,
):
    import app.services.session_permission_runtime as permission_runtime
    import app.services.session_v2_persistence as persistence

    async def fake_get(**_kwargs):
        return session, agent, "session_owner"

    async def fake_authority(*_args, **_kwargs):
        return authority

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get)
    monkeypatch.setattr(persistence, "resolve_session_mutation_authority", fake_authority)
    monkeypatch.setattr(permission_runtime, "resolve_session_tool_permission", resolver)


def test_resolve_session_permission_routes_to_native_control_and_same_run_resume(monkeypatch):
    import app.services.runtime_task_worker as runtime_task_worker
    from app.services.session_permission_runtime import SessionPermissionResolutionReceipt

    agent_id, tenant_id, user_id, session_id, permission_id, invocation_id, run_id = (uuid4() for _ in range(7))
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=user_id)
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id)
    authority = SimpleNamespace(
        tenant_id=tenant_id,
        agent_id=agent_id,
        principal_id=user_id,
        session_id=session_id,
    )
    captured = {}

    async def fake_resolve(db, **kwargs):
        captured.update({"db": db, **kwargs})
        return SessionPermissionResolutionReceipt(
            schema="hive.session_permission_resolution.v2",
            status="resolved",
            permission_request_id=str(permission_id),
            invocation_id=str(invocation_id),
            control_id=str(uuid4()),
            run_id=str(run_id),
            run_status="resumable",
            result_event_id=str(uuid4()),
            retryable=False,
            recovery_action="resume_same_runtime_task",
        )

    notifications = []

    async def fake_notify(**kwargs):
        notifications.append(kwargs)

    db = _FakeDB(session)
    _install_native_permission_endpoint_fakes(
        monkeypatch,
        session=session,
        agent=agent,
        authority=authority,
        resolver=fake_resolve,
    )
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", fake_notify)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_id}/resolve",
        json={"action": "allow_once"},
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == str(run_id)
    assert response.json()["status"] == "resolved"
    assert captured["authority"] is authority
    assert captured["permission_request_id"] == permission_id
    assert captured["decision"] == "allow_once"
    assert notifications == [{"reason": "session_permission_resolved", "runtime_task_id": str(run_id)}]


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("pending_session_permission_not_found", 404),
        ("tool_permission_request_expired", 410),
        ("destructive_permission_must_be_allow_once", 400),
        ("tool_permission_response_requires_waiting_pre_effect_invocation", 409),
    ],
)
def test_resolve_session_permission_maps_native_typed_rejections(
    monkeypatch,
    code,
    expected_status,
):
    agent_id, tenant_id, user_id, session_id, permission_id = (uuid4() for _ in range(5))
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=user_id)
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id)
    authority = SimpleNamespace(
        tenant_id=tenant_id,
        agent_id=agent_id,
        principal_id=user_id,
        session_id=session_id,
    )

    async def fake_resolve(*_args, **_kwargs):
        raise ValueError(code)

    db = _FakeDB(session)
    _install_native_permission_endpoint_fakes(
        monkeypatch,
        session=session,
        agent=agent,
        authority=authority,
        resolver=fake_resolve,
    )
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_id}/resolve",
        json={"action": "allow_session"},
    )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == code


def test_resolve_session_permission_exposes_idempotency_conflict_receipt(monkeypatch):
    from app.services.session_v2_persistence import IdempotencyConflict

    agent_id, tenant_id, user_id, session_id, permission_id, command_id = (uuid4() for _ in range(6))
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=user_id)
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id)
    authority = SimpleNamespace(
        tenant_id=tenant_id,
        agent_id=agent_id,
        principal_id=user_id,
        session_id=session_id,
    )

    async def fake_resolve(*_args, **_kwargs):
        raise IdempotencyConflict(command=SimpleNamespace(id=command_id, receipt_ref="session-control:existing"))

    db = _FakeDB(session)
    _install_native_permission_endpoint_fakes(
        monkeypatch,
        session=session,
        agent=agent,
        authority=authority,
        resolver=fake_resolve,
    )
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/permissions/{permission_id}/resolve",
        json={"action": "deny"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "idempotency_conflict",
        "command_id": str(command_id),
        "receipt_ref": "session-control:existing",
    }


def test_steer_session_turn_routes_to_runtime_service(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_active(**_kwargs):
        return {"run_id": str(uuid4()), "turn_id": "turn-1", "status": "running"}

    async def fake_steer(**kwargs):
        captured.update(kwargs)
        return {
            "input_id": str(kwargs["body"].input_id),
            "status": "queued",
        }

    monkeypatch.setattr(chat_sessions_api, "get_active_web_chat_run", fake_active)
    monkeypatch.setattr(chat_sessions_api, "_submit_session_human_input", fake_steer)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/turns/steer",
        json={"content": "Narrow the answer.", "expected_turn_id": "turn-1"},
    )

    assert response.status_code == 200
    assert response.json()["steer_strategy"] == "session_v2_durable_mailbox"
    assert captured["body"].content_parts == [{"type": "text", "text": "Narrow the answer."}]
    assert captured["body"].expected_turn_id == "turn-1"


def test_thread_turn_interrupt_alias_routes_to_typed_control_input_service(monkeypatch):
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_cancel(**kwargs):
        captured.update(kwargs)
        return {"control_id": "control-1", "status": "applying"}

    monkeypatch.setattr(chat_sessions_api, "_cancel_session_run_v2", fake_cancel)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(f"/agents/{agent_id}/threads/{session_id}/turns/{run_id}/interrupt")

    assert response.status_code == 200
    assert response.json()["status"] == "applying"
    assert captured["agent_id"] == agent_id
    assert captured["run_id"] == run_id
    assert captured["current_user"] is user


def test_unified_session_input_accepts_admits_then_queues_explicit_intent(monkeypatch):
    from app.services.session_input_admission import AdmissionOutcome
    from app.services.session_v2_persistence import HumanInputReceipt

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    input_id = uuid4()
    command_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=user_id)
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    row = SimpleNamespace(
        id=input_id,
        command_id=command_id,
        intent="steer_current_turn",
        revision=1,
        status="queued",
        queue_priority="next",
        queue_ordinal=1,
        target_turn_id="turn-1",
        target_run_id=run_id,
        bound_round_id=None,
        rolled_over_to_turn_id=None,
    )

    class _InputDB(_FakeDB):
        def __init__(self, session):
            super().__init__(session)
            self.input_gets = 0

        async def get(self, _model, key):
            assert key == input_id
            self.input_gets += 1
            return None if self.input_gets == 1 else row

        async def rollback(self):
            return None

    db = _InputDB(session)
    order = []

    async def fake_get(**_kwargs):
        return session, agent, "session_owner"

    async def fake_resolve(*_args, **_kwargs):
        return SimpleNamespace(tenant_id=tenant_id, agent_id=agent_id, principal_id=user_id, session_id=session_id)

    async def fake_accept(*_args, **_kwargs):
        order.append("accept")
        return HumanInputReceipt(
            command_id=command_id,
            input_id=input_id,
            idempotency_key="input-key",
            intent="steer_current_turn",
            revision=1,
            status="accepted",
            accepted_sequence=10,
            queue_priority="next",
            queue_ordinal=1,
        )

    async def fake_admit(*_args, **_kwargs):
        order.append("admit")
        return AdmissionOutcome(uuid4(), input_id, uuid4(), "admitted")

    async def fake_dispatch(*_args, **_kwargs):
        order.append("dispatch")
        return SimpleNamespace(state="mailbox_queued", receipt={"kind": "mailbox"})

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get)
    monkeypatch.setattr("app.services.session_live_input.resolve_session_mutation_authority", fake_resolve)
    monkeypatch.setattr("app.services.session_live_input.accept_human_input", fake_accept)
    monkeypatch.setattr("app.services.session_input_admission.run_user_prompt_admission", fake_admit)
    monkeypatch.setattr("app.services.session_input_dispatch.dispatch_admitted_input_fast_path", fake_dispatch)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/inputs",
        json={
            "kind": "steer_current_turn",
            "input_id": str(input_id),
            "idempotency_key": "input-key",
            "content_parts": [{"type": "text", "text": "Narrow it"}],
            "expected_turn_id": "turn-1",
            "expected_run_id": str(run_id),
            "terminal_fallback": "queue_next_turn",
        },
    )

    assert response.status_code == 200
    assert order == ["accept", "admit", "dispatch"]
    assert response.json()["status"] == "queued"
    assert response.json()["dispatch_status"] == "mailbox_queued"
    assert response.json()["accepted_sequence"] == 10


def test_unified_session_input_blocked_hook_never_dispatches_runtime(monkeypatch):
    from app.services.session_input_admission import AdmissionOutcome
    from app.services.session_v2_persistence import HumanInputReceipt

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    input_id = uuid4()
    command_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=user_id)
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    row = SimpleNamespace(
        id=input_id,
        command_id=command_id,
        intent="start_turn",
        revision=1,
        status="rejected",
        queue_priority="now",
        queue_ordinal=1,
        target_turn_id=None,
        target_run_id=None,
        bound_round_id=None,
        rolled_over_to_turn_id=None,
    )

    class _InputDB(_FakeDB):
        def __init__(self, session):
            super().__init__(session)
            self.input_gets = 0

        async def get(self, _model, _key):
            self.input_gets += 1
            return None if self.input_gets == 1 else row

        async def rollback(self):
            return None

    db = _InputDB(session)

    async def fake_get(**_kwargs):
        return session, agent, "session_owner"

    async def fake_resolve(*_args, **_kwargs):
        return SimpleNamespace(tenant_id=tenant_id, agent_id=agent_id, principal_id=user_id, session_id=session_id)

    async def fake_accept(*_args, **_kwargs):
        return HumanInputReceipt(
            command_id=command_id,
            input_id=input_id,
            idempotency_key="blocked-key",
            intent="start_turn",
            revision=1,
            status="accepted",
            accepted_sequence=1,
            queue_priority="now",
            queue_ordinal=1,
        )

    async def fake_admit(*_args, **_kwargs):
        return AdmissionOutcome(uuid4(), input_id, uuid4(), "rejected", "user_prompt_submit_blocked")

    async def forbidden_start(**_kwargs):
        raise AssertionError("blocked input must not start a RuntimeTask")

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get)
    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", forbidden_start)
    monkeypatch.setattr("app.services.session_live_input.resolve_session_mutation_authority", fake_resolve)
    monkeypatch.setattr("app.services.session_live_input.accept_human_input", fake_accept)
    monkeypatch.setattr("app.services.session_input_admission.run_user_prompt_admission", fake_admit)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/inputs",
        json={
            "kind": "start_turn",
            "input_id": str(input_id),
            "idempotency_key": "blocked-key",
            "content_parts": [{"type": "text", "text": "blocked"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["run"] is None
    assert response.json()["reason_code"] == "user_prompt_submit_blocked"


def test_unified_replacement_persists_requested_saga_for_durable_worker_progression(
    monkeypatch,
):
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_input_admission import AdmissionOutcome
    from app.services.session_turn_replacement import TurnReplacementReceipt
    from app.services.session_v2_persistence import HumanInputReceipt

    agent_id, tenant_id, user_id, session_id, run_id, input_id, command_id = (uuid4() for _ in range(7))
    saga_id, saga_command_id = uuid4(), uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=user_id)
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    row = SimpleNamespace(
        id=input_id,
        command_id=command_id,
        intent="interrupt_and_replace",
        revision=1,
        status="accepted",
        queue_priority="now",
        queue_ordinal=1,
        target_turn_id="turn-old",
        target_run_id=run_id,
        bound_round_id=None,
        rolled_over_to_turn_id=None,
    )

    class _ReplacementDB(_FakeDB):
        def __init__(self, session):
            super().__init__(session)
            self.input_gets = 0

        async def get(self, model, key):
            if model is SessionTurnInput:
                self.input_gets += 1
                return None if self.input_gets == 1 else row
            if model is RuntimeTask:
                return SimpleNamespace(id=key, status="running", completed_at=None)
            return None

        async def rollback(self):
            return None

    db = _ReplacementDB(session)
    order = []

    async def fake_get(**_kwargs):
        return session, agent, "session_owner"

    async def fake_resolve(*_args, **_kwargs):
        return SimpleNamespace(tenant_id=tenant_id, agent_id=agent_id, principal_id=user_id, session_id=session_id)

    async def fake_accept(*_args, **_kwargs):
        order.append("accept_input")
        return HumanInputReceipt(
            command_id=command_id,
            input_id=input_id,
            idempotency_key="replace-key",
            intent="interrupt_and_replace",
            revision=1,
            status="accepted",
            accepted_sequence=1,
            queue_priority="now",
            queue_ordinal=1,
        )

    async def fake_admit(*_args, **_kwargs):
        order.append("admit_input")
        return AdmissionOutcome(uuid4(), input_id, uuid4(), "admitted")

    def saga(state, *, replayed=False):
        return TurnReplacementReceipt(
            saga_id=saga_id,
            parent_command_id=command_id,
            saga_command_id=saga_command_id,
            replacement_input_id=input_id,
            replacement_turn_id="turn-new",
            old_run_id=run_id,
            old_turn_id="turn-old",
            state=state,
            cancel_control_id=uuid4() if state != "requested" else None,
            cancel_command_id=uuid4() if state != "requested" else None,
            replayed=replayed,
        )

    async def fake_dispatch(*_args, **_kwargs):
        order.append("dispatch_input")
        receipt = saga("requested")
        return SimpleNamespace(
            state="replacement_requested",
            receipt={
                "kind": "replacement",
                "saga_id": str(receipt.saga_id),
                "parent_command_id": str(receipt.parent_command_id),
                "saga_command_id": str(receipt.saga_command_id),
                "replacement_input_id": str(receipt.replacement_input_id),
                "replacement_turn_id": receipt.replacement_turn_id,
                "old_run_id": str(receipt.old_run_id),
                "old_turn_id": receipt.old_turn_id,
                "state": receipt.state,
                "cancel_control_id": None,
                "cancel_command_id": None,
                "replayed": False,
            },
        )

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get)
    monkeypatch.setattr("app.services.session_live_input.resolve_session_mutation_authority", fake_resolve)
    monkeypatch.setattr("app.services.session_live_input.accept_human_input", fake_accept)
    monkeypatch.setattr("app.services.session_input_admission.run_user_prompt_admission", fake_admit)
    monkeypatch.setattr("app.services.session_input_dispatch.dispatch_admitted_input_fast_path", fake_dispatch)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/inputs",
        json={
            "kind": "interrupt_and_replace",
            "input_id": str(input_id),
            "idempotency_key": "replace-key",
            "content_parts": [{"type": "text", "text": "Replace it"}],
            "expected_turn_id": "turn-old",
            "expected_run_id": str(run_id),
        },
    )

    assert response.status_code == 200
    assert order == ["accept_input", "admit_input", "dispatch_input"]
    assert response.json()["dispatch_status"] == "replacement_requested"
    assert response.json()["replacement"]["state"] == "requested"
    assert "pending" not in response.json()["dispatch_status"]


def test_unified_fork_dispatches_real_branch_service_receipt_instead_of_pending_placeholder(monkeypatch):
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_fork_input import ForkSideThreadReceipt
    from app.services.session_input_admission import AdmissionOutcome
    from app.services.session_v2_persistence import HumanInputReceipt

    agent_id, tenant_id, user_id, session_id, input_id, command_id = (uuid4() for _ in range(6))
    branch_session_id, branch_run_id = uuid4(), uuid4()
    admission_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=user_id)
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id, transcript_metadata_json={})
    row = SimpleNamespace(
        id=input_id,
        command_id=command_id,
        intent="fork_side_thread",
        revision=1,
        status="applied",
        queue_priority="later",
        queue_ordinal=1,
        target_turn_id=None,
        target_run_id=None,
        bound_round_id=None,
        rolled_over_to_turn_id=None,
    )

    class _ForkDB(_FakeDB):
        def __init__(self, session):
            super().__init__(session)
            self.input_gets = 0

        async def get(self, model, _key):
            if model is SessionTurnInput:
                self.input_gets += 1
                return None if self.input_gets == 1 else row
            return None

        async def rollback(self):
            return None

    db = _ForkDB(session)
    dispatched = []

    async def fake_get(**_kwargs):
        return session, agent, "session_owner"

    async def fake_resolve(*_args, **_kwargs):
        return SimpleNamespace(tenant_id=tenant_id, agent_id=agent_id, principal_id=user_id, session_id=session_id)

    async def fake_accept(*_args, **_kwargs):
        return HumanInputReceipt(
            command_id=command_id,
            input_id=input_id,
            idempotency_key="fork-key",
            intent="fork_side_thread",
            revision=1,
            status="accepted",
            accepted_sequence=3,
            queue_priority="later",
            queue_ordinal=1,
        )

    async def fake_admit(*_args, **_kwargs):
        return AdmissionOutcome(admission_id, input_id, uuid4(), "admitted")

    async def fake_dispatch(*_args, **kwargs):
        dispatched.append(kwargs)
        receipt = ForkSideThreadReceipt(
            command_id,
            input_id,
            "applied",
            branch_session_id=branch_session_id,
            branch_run_id=branch_run_id,
        )
        return SimpleNamespace(
            state="fork_run_queued",
            receipt={
                "kind": "fork",
                "command_id": str(receipt.command_id),
                "input_id": str(receipt.input_id),
                "status": receipt.status,
                "branch_session_id": str(receipt.branch_session_id),
                "branch_run_id": str(receipt.branch_run_id),
            },
        )

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get)
    monkeypatch.setattr("app.services.session_live_input.resolve_session_mutation_authority", fake_resolve)
    monkeypatch.setattr("app.services.session_live_input.accept_human_input", fake_accept)
    monkeypatch.setattr("app.services.session_input_admission.run_user_prompt_admission", fake_admit)
    monkeypatch.setattr("app.services.session_input_dispatch.dispatch_admitted_input_fast_path", fake_dispatch)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/inputs",
        json={
            "kind": "fork_side_thread",
            "input_id": str(input_id),
            "idempotency_key": "fork-key",
            "content_parts": [{"type": "text", "text": "Explore this branch"}],
            "fork_after_sequence": 3,
        },
    )

    assert response.status_code == 200
    assert response.json()["dispatch_status"] == "fork_run_queued"
    assert response.json()["fork"]["branch_session_id"] == str(branch_session_id)
    assert "pending" not in response.json()["dispatch_status"]
    assert dispatched[0]["admission_id"] == admission_id


def test_cancel_session_run_returns_durable_applying_receipt_and_ack_replay_does_not_resignal(monkeypatch):
    from app.services.session_control_input import ControlInputReceipt

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    control_id = uuid.uuid5(run_id, f"cancel:{user_id}")
    command_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=user_id)
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    begin_calls = 0
    signals = 0

    async def fake_get(**_kwargs):
        return session, agent, "session_owner"

    async def fake_resolve(*_args, **_kwargs):
        return SimpleNamespace(tenant_id=tenant_id, agent_id=agent_id, principal_id=user_id, session_id=session_id)

    async def fake_accept(*_args, **_kwargs):
        return ControlInputReceipt(command_id, control_id, "accepted", accepted_sequence=4, replayed=begin_calls > 0)

    async def fake_begin(*_args, **_kwargs):
        nonlocal begin_calls
        begin_calls += 1
        return ControlInputReceipt(
            command_id,
            control_id,
            "applying",
            accepted_sequence=4,
            replayed=begin_calls > 1,
        )

    async def fake_signal(**_kwargs):
        nonlocal signals
        signals += 1

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get)
    monkeypatch.setattr("app.services.web_chat_runtime.signal_web_chat_cancel", fake_signal)
    monkeypatch.setattr("app.services.session_live_input.resolve_session_mutation_authority", fake_resolve)
    monkeypatch.setattr("app.services.session_control_input.accept_cancel_control_input", fake_accept)
    monkeypatch.setattr("app.services.session_control_input.begin_cancel_control_input", fake_begin)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    first = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel",
        headers={"Idempotency-Key": "stop-key"},
    )
    replay = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel",
        headers={"Idempotency-Key": "stop-key"},
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["status"] == replay.json()["status"] == "applying"
    assert first.json()["accepted"] is replay.json()["accepted"] is True
    assert first.json()["run_id"] == replay.json()["run_id"] == str(run_id)
    assert first.json()["control_id"] == replay.json()["control_id"] == str(control_id)
    assert replay.json()["replayed"] is True
    assert signals == 1


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
