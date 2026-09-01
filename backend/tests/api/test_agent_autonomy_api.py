from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

import app.api.autonomy as autonomy_api
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    pass


class _AuditDB:
    def __init__(self):
        self.added = []
        self.flushed = False

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True


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


class _PolicySnapshot:
    def __init__(self, *, version=1, source="migration"):
        self.version = version
        self.revision_id = uuid4()
        self.content_hash = f"hash-v{version}"
        self.source = source
        self.valid = True
        self.error_code = None

    def response_payload(self, *, can_manage):
        return {
            "schema": "hive.owner_action_policy.v1",
            "actions": {
                "tool.external_effect": "confirm_first",
                "tool.local_read": "full_authority",
                "tool.local_write": "full_authority",
            },
            "version": self.version,
            "revision_id": str(self.revision_id),
            "content_hash": self.content_hash,
            "source": self.source,
            "valid": self.valid,
            "error_code": self.error_code,
            "can_manage": can_manage,
        }


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
            agent or SimpleNamespace(id=agent_id, tenant_id=_user.tenant_id, creator_id=uuid4()),
            access_level,
        )

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(autonomy_api, "check_agent_access", allow_access)
    monkeypatch.setattr(autonomy_api, "check_agent_operator_reachability", allow_access)
    return TestClient(app), user


def _allow_runtime_resource(monkeypatch, *, status="completed", artifact_bound=True, artifact_projected=True):
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    async def allow(**kwargs):
        task_id = UUID(str(kwargs["runtime_task_id"]))
        metadata = {"output_artifact": trigger_output_artifact_ref(str(task_id))} if artifact_bound else {}
        return (
            SimpleNamespace(
                id=task_id,
                task_type="trigger",
                status=status,
                metadata_json=metadata,
            ),
            SimpleNamespace(authority_source="root_owner"),
        )

    monkeypatch.setattr(autonomy_api, "_authorize_runtime_task_read", allow)

    async def projection_delivered(_db, _task):
        return artifact_projected

    monkeypatch.setattr(autonomy_api, "trigger_artifact_projection_delivered", projection_delivered)


def test_agent_autonomy_overview_is_agent_scoped_and_readable_by_member(monkeypatch):
    agent_id = uuid4()
    captured = {}

    async def fake_overview(*, db, agent, lookback_hours, include_diagnostics, principal, resource_user, agent_access):
        captured["agent"] = agent
        captured["lookback_hours"] = lookback_hours
        captured["include_diagnostics"] = include_diagnostics
        captured["principal"] = principal
        captured["resource_user"] = resource_user
        captured["agent_access"] = agent_access
        return {
            "agent_id": str(agent.id),
            "lookback_hours": lookback_hours,
            "totals": {"triggers": 1, "recent_attempts": 0, "findings": 0},
            "triggers": [{"id": "trigger-1", "display_kind": "scheduled_job", "attention_state": "active"}],
            "recent_attempts": [],
            "findings": [],
        }

    monkeypatch.setattr(autonomy_api, "build_agent_autonomy_overview", fake_overview)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/autonomy/overview", params={"lookback_hours": 6})

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_id"] == str(agent_id)
    assert payload["triggers"][0]["display_kind"] == "scheduled_job"
    assert captured["lookback_hours"] == 6
    assert captured["include_diagnostics"] is False
    assert captured["principal"].requester_user_id == _user.id
    assert captured["resource_user"] is _user
    assert captured["agent_access"][1] == "manage"


def test_agent_autonomy_diagnostics_explicitly_includes_diagnostics(monkeypatch):
    agent_id = uuid4()
    captured = {}

    async def fake_overview(*, db, agent, lookback_hours, include_diagnostics, principal, resource_user, agent_access):
        captured["include_diagnostics"] = include_diagnostics
        captured["principal"] = principal
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


def test_owner_action_policy_read_is_business_level_and_access_scoped(monkeypatch):
    agent_id = uuid4()
    captured = {}

    async def fake_load(db, *, agent_id, tenant_id, create_default):
        captured.update(
            {
                "db": db,
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "create_default": create_default,
            }
        )
        return _PolicySnapshot()

    monkeypatch.setattr(autonomy_api, "load_owner_action_policy", fake_load)
    client, user = _client(monkeypatch, access_level="use")

    response = client.get(f"/agents/{agent_id}/autonomy/action-policy")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "hive.owner_action_policy.v1"
    assert payload["can_manage"] is False
    assert payload["actions"]["tool.external_effect"] == "confirm_first"
    assert "handler_name" not in str(payload)
    assert "hook" not in str(payload).lower()
    assert captured["agent_id"] == agent_id
    assert captured["tenant_id"] == user.tenant_id
    assert captured["create_default"] is True


def test_owner_action_policy_update_requires_manage_and_writes_audit(monkeypatch):
    agent_id = uuid4()
    actions = {
        "tool.external_effect": "full_authority",
        "tool.local_read": "full_authority",
        "tool.local_write": "confirm_first",
    }
    save_calls = []

    async def fake_save(db, **kwargs):
        save_calls.append({"db": db, **kwargs})
        return _PolicySnapshot(version=2, source="user")

    monkeypatch.setattr(autonomy_api, "save_owner_action_policy", fake_save)

    denied_client, _ = _client(monkeypatch, access_level="use")
    denied = denied_client.put(
        f"/agents/{agent_id}/autonomy/action-policy",
        json={"actions": actions, "expected_version": 1},
    )
    assert denied.status_code == 403
    assert save_calls == []

    audit_db = _AuditDB()
    manager_client, manager = _client(monkeypatch, db=audit_db, access_level="manage")
    allowed = manager_client.put(
        f"/agents/{agent_id}/autonomy/action-policy",
        json={"actions": actions, "expected_version": 1},
    )

    assert allowed.status_code == 200
    assert allowed.json()["version"] == 2
    assert allowed.json()["can_manage"] is True
    assert save_calls[0]["agent_id"] == agent_id
    assert save_calls[0]["tenant_id"] == manager.tenant_id
    assert save_calls[0]["changed_by_user_id"] == manager.id
    assert save_calls[0]["expected_version"] == 1
    audit = audit_db.added[0]
    assert audit.action == "agent.owner_action_policy.updated"
    assert audit.user_id == manager.id
    assert audit.agent_id == agent_id
    assert audit.details["revision_version"] == 2
    assert audit.details["content_hash"] == "hash-v2"
    assert audit_db.flushed is True


def test_owner_action_policy_update_rejects_non_exact_action_contract(monkeypatch):
    agent_id = uuid4()
    client, _ = _client(monkeypatch, access_level="manage")

    response = client.put(
        f"/agents/{agent_id}/autonomy/action-policy",
        json={
            "actions": {
                "tool.external_effect": "confirm_first",
                "tool.local_read": "full_authority",
            },
            "expected_version": 1,
        },
    )

    assert response.status_code == 422
    assert "exact action ids" in response.json()["detail"]


def test_owner_action_policy_mutations_require_optimistic_version_binding(monkeypatch):
    agent_id = uuid4()
    client, _ = _client(monkeypatch, access_level="manage")
    actions = {
        "tool.external_effect": "confirm_first",
        "tool.local_read": "full_authority",
        "tool.local_write": "full_authority",
    }

    update = client.put(
        f"/agents/{agent_id}/autonomy/action-policy",
        json={"actions": actions},
    )
    rollback = client.post(
        f"/agents/{agent_id}/autonomy/action-policy/rollback",
        json={"target_version": 1, "reason": "Restore approved policy"},
    )

    assert update.status_code == 422
    assert rollback.status_code == 422


def test_owner_action_policy_history_is_manage_only_and_tenant_scoped(monkeypatch):
    agent_id = uuid4()
    history_calls = []

    async def fake_history(db, entity_type, entity_id, *, limit, tenant_id):
        history_calls.append(
            {
                "db": db,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "limit": limit,
                "tenant_id": tenant_id,
            }
        )
        return [{"version": 2, "content_hash": "hash-v2", "is_active": True}]

    monkeypatch.setattr(autonomy_api, "get_history", fake_history)
    denied_client, _ = _client(monkeypatch, access_level="use")
    denied = denied_client.get(f"/agents/{agent_id}/autonomy/action-policy/history")
    assert denied.status_code == 403
    assert history_calls == []

    manager_client, manager = _client(monkeypatch, access_level="manage")
    allowed = manager_client.get(
        f"/agents/{agent_id}/autonomy/action-policy/history",
        params={"limit": 7},
    )

    assert allowed.status_code == 200
    assert allowed.json()["items"][0]["version"] == 2
    assert history_calls[0]["entity_type"] == "owner_action_policy"
    assert history_calls[0]["entity_id"] == agent_id
    assert history_calls[0]["limit"] == 7
    assert history_calls[0]["tenant_id"] == manager.tenant_id


def test_owner_action_policy_rollback_requires_manage_and_audits_new_revision(monkeypatch):
    agent_id = uuid4()
    rollback_calls = []

    async def fake_rollback(db, **kwargs):
        rollback_calls.append({"db": db, **kwargs})
        return _PolicySnapshot(version=4, source="rollback")

    monkeypatch.setattr(autonomy_api, "rollback_owner_action_policy", fake_rollback)
    audit_db = _AuditDB()
    client, manager = _client(monkeypatch, db=audit_db, access_level="manage")

    response = client.post(
        f"/agents/{agent_id}/autonomy/action-policy/rollback",
        json={"target_version": 2, "expected_version": 3, "reason": "Restore approved policy"},
    )

    assert response.status_code == 200
    assert response.json()["version"] == 4
    assert rollback_calls[0]["target_version"] == 2
    assert rollback_calls[0]["expected_version"] == 3
    assert rollback_calls[0]["changed_by_user_id"] == manager.id
    audit = audit_db.added[0]
    assert audit.action == "agent.owner_action_policy.rolled_back"
    assert audit.details["target_version"] == 2
    assert audit.details["revision_version"] == 4


def test_agent_runtime_tasks_endpoint_passes_filters(monkeypatch):
    agent_id = uuid4()
    captured = {}

    async def fake_runtime_tasks(
        *, db, agent_id, task_type, trigger_id, status, limit, include_diagnostics, **authority
    ):
        captured.update(
            {
                "agent_id": agent_id,
                "task_type": task_type,
                "trigger_id": trigger_id,
                "status": status,
                "limit": limit,
                "include_diagnostics": include_diagnostics,
                **authority,
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
    assert captured["principal"].requester_user_id == _user.id
    assert captured["allow_operator_override"] is False


def test_runtime_task_operator_override_requires_independent_inspection_grant(monkeypatch):
    agent_id = uuid4()

    async def fake_runtime_tasks(**_kwargs):
        return []

    monkeypatch.setattr(autonomy_api, "list_agent_runtime_task_views", fake_runtime_tasks)

    async def deny_operator_inspection(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="Active operator.inspect permission is required")

    monkeypatch.setattr(autonomy_api, "authorize_agent_operator_inspection", deny_operator_inspection)

    member_client, _ = _client(monkeypatch, access_level="use")
    denied = member_client.get(
        f"/agents/{agent_id}/runtime-tasks",
        params={"operator_override": "true", "operator_reason": "incident"},
    )
    assert denied.status_code == 403

    manager_client, _ = _client(monkeypatch, access_level="manage")
    still_denied = manager_client.get(
        f"/agents/{agent_id}/runtime-tasks",
        params={"operator_override": "true", "operator_reason": "incident"},
    )
    assert still_denied.status_code == 403

    inspection_calls = []

    async def allow_operator_inspection(_db, **kwargs):
        inspection_calls.append(kwargs)
        if not str(kwargs.get("reason") or "").strip():
            raise HTTPException(status_code=403, detail="Operator View requires an audit reason")
        return "operator_inspect_grant"

    monkeypatch.setattr(autonomy_api, "authorize_agent_operator_inspection", allow_operator_inspection)
    manager_client, _manager = _client(monkeypatch, access_level="use")
    missing_reason = manager_client.get(
        f"/agents/{agent_id}/runtime-tasks",
        params={"operator_override": "true"},
    )
    assert missing_reason.status_code == 403
    allowed = manager_client.get(
        f"/agents/{agent_id}/runtime-tasks",
        params={"operator_override": "true", "operator_reason": "incident investigation INC-42"},
    )
    assert allowed.status_code == 200
    assert inspection_calls[-1]["action"] == "runtime_task_collection:read"
    assert inspection_calls[-1]["reason"] == "incident investigation INC-42"


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
    _allow_runtime_resource(monkeypatch)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/runtime-artifacts/{runtime_task_id}")

    assert response.status_code == 200
    assert response.json()["summary"] == "Report delivered."
    assert captured["agent_id"] == agent_id
    assert UUID(captured["runtime_task_id"]) == UUID(runtime_task_id)
    assert captured["include_diagnostics"] is False


def test_runtime_artifact_allows_failed_canonical_task_after_projection(monkeypatch):
    agent_id = uuid4()
    task_id = uuid4()
    read_called = False

    async def fake_artifact(**_kwargs):
        nonlocal read_called
        read_called = True
        return {"summary": "partial evidence"}

    monkeypatch.setattr(autonomy_api, "read_agent_trigger_artifact_view", fake_artifact)
    _allow_runtime_resource(monkeypatch, status="failed", artifact_bound=True)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/runtime-artifacts/{task_id}")

    assert response.status_code == 200
    assert response.json()["summary"] == "partial evidence"
    assert read_called is True


@pytest.mark.parametrize(
    ("artifact_bound", "artifact_projected"),
    [(True, False), (False, True)],
)
def test_runtime_artifact_requires_canonical_binding_and_delivered_projection(
    monkeypatch,
    artifact_bound,
    artifact_projected,
):
    agent_id = uuid4()
    task_id = uuid4()
    read_called = False

    async def fake_artifact(**_kwargs):
        nonlocal read_called
        read_called = True
        return {"summary": "uncommitted"}

    monkeypatch.setattr(autonomy_api, "read_agent_trigger_artifact_view", fake_artifact)
    _allow_runtime_resource(
        monkeypatch,
        status="needs_reconciliation",
        artifact_bound=artifact_bound,
        artifact_projected=artifact_projected,
    )
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/runtime-artifacts/{task_id}")

    assert response.status_code == 404
    assert read_called is False


def test_runtime_artifact_rejects_foreign_root_principal_before_reading_file(monkeypatch):
    agent_id = uuid4()
    tenant_id = uuid4()
    task_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id, username="member")
    runtime_task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        root_user_id=uuid4(),
        root_session_id=str(uuid4()),
        root_runtime_task_id=task_id,
        delegation_chain_json=[{"agent_id": str(agent_id)}],
        metadata_json={},
    )
    db = _SessionDB(runtime_task)
    read_called = False

    async def fake_artifact(**_kwargs):
        nonlocal read_called
        read_called = True
        return {"summary": "foreign"}

    monkeypatch.setattr(autonomy_api, "read_agent_trigger_artifact_view", fake_artifact)
    client, _ = _client(monkeypatch, db=db, user=user, access_level="use")

    response = client.get(f"/agents/{agent_id}/runtime-artifacts/{task_id}")

    assert response.status_code == 403
    assert read_called is False


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
    _allow_runtime_resource(monkeypatch)
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
    _allow_runtime_resource(monkeypatch)
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


def test_agent_session_work_ledger_endpoint_requires_explicit_manager_override(monkeypatch):
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

    assert response.status_code == 403

    inspection_calls = []

    async def fake_operator_inspection(_db, **kwargs):
        inspection_calls.append(kwargs)
        return "operator_inspect_grant"

    monkeypatch.setattr(autonomy_api, "authorize_agent_operator_inspection", fake_operator_inspection)
    response = client.get(
        f"/agents/{agent_id}/sessions/{session_id}/work-ledger",
        params={
            "operator_override": "true",
            "operator_reason": "Reviewing a failed delegated run",
        },
    )

    assert response.status_code == 200
    assert response.json()["todo_items"][0]["title"] == "Manager visible todo"
    assert response.json()["operator_view"] is True
    assert inspection_calls[0]["action"] == "work_ledger:read"
