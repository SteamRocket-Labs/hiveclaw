from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)


class _QueueDB:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _statement):
        if not self.values:
            raise AssertionError("Unexpected database query")
        return _ListResult(self.values.pop(0))


@pytest.mark.asyncio
async def test_task_list_consumes_resource_authority_and_marks_operator_view(monkeypatch):
    import app.api.tasks as tasks_api
    from app.models.task import Task

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    other_id = uuid4()
    now = datetime.now(UTC)

    def task(owner_id):
        return Task(
            id=uuid4(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            title="owned task",
            description=None,
            type="todo",
            status="pending",
            priority="medium",
            assignee="self",
            created_by=owner_id,
            request_id=f"request-{uuid4().hex}",
            request_hash="a" * 64,
            authority_state="owned",
            execution_attempt=0,
            created_at=now,
            updated_at=now,
        )

    mine, foreign = task(owner_id), task(other_id)
    db = _QueueDB([mine, foreign], [SimpleNamespace(id=owner_id, username="owner")])
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=tenant_id, department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    calls = []

    async def fake_access(*_args):
        return agent, "use"

    async def fake_filter(_db, _user, **kwargs):
        calls.append(kwargs)
        decision = SimpleNamespace(authority_source="resource_owner", operator_view=False)
        return [(mine, decision)]

    monkeypatch.setattr(tasks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(tasks_api, "filter_authorized_resources", fake_filter, raising=False)

    payload = await tasks_api.list_tasks(
        agent_id=agent_id,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=db,
    )

    assert [row.id for row in payload] == [mine.id]
    assert payload[0].authority_source == "resource_owner"
    assert payload[0].operator_view is False
    assert calls[0]["resource_kind"] == "task"


@pytest.mark.asyncio
async def test_activity_and_failure_surfaces_filter_before_consumption(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    now = datetime.now(UTC)
    owned = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        owner_user_id=owner_id,
        root_session_id=None,
        authority_state="owned",
        action_type="error",
        summary="Owned failure",
        detail_json={"tool_name": "read_file", "provider": "runtime"},
        related_id=None,
        created_at=now,
    )
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=tenant_id, department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    async def fake_access(*_args):
        return agent, "use"

    async def no_grants(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(activity_api, "check_agent_access", fake_access)
    monkeypatch.setattr(activity_api, "load_explicit_resource_grant_ids", no_grants)

    activity = await activity_api.get_agent_activity(
        agent_id=agent_id,
        limit=50,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=_QueueDB([owned]),
    )
    failures = await activity_api.get_agent_tool_failure_summary(
        agent_id=agent_id,
        hours=24,
        limit=100,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=_QueueDB([owned]),
    )

    assert [row["summary"] for row in activity] == ["Owned failure"]
    assert activity[0]["authority_source"] == "resource_owner"
    assert failures["total_errors"] == 1
    assert failures["recent_errors"][0]["summary"] == "Owned failure"


@pytest.mark.asyncio
async def test_activity_limit_is_applied_after_resource_authority_filter(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    now = datetime.now(UTC)
    foreign = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        owner_user_id=uuid4(),
        root_session_id=None,
        authority_state="owned",
        action_type="tool_call",
        summary="Foreign newest row",
        detail_json={},
        related_id=None,
        created_at=now,
    )
    owned = SimpleNamespace(
        **{
            **foreign.__dict__,
            "id": uuid4(),
            "owner_user_id": owner_id,
            "summary": "Owned older row",
        }
    )
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=tenant_id, department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    async def fake_access(*_args):
        return agent, "use"

    async def no_grants(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(activity_api, "check_agent_access", fake_access)
    monkeypatch.setattr(activity_api, "load_explicit_resource_grant_ids", no_grants)

    payload = await activity_api.get_agent_activity(
        agent_id=agent_id,
        limit=1,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=_QueueDB([owned]),
    )

    assert [row["summary"] for row in payload] == ["Owned older row"]


@pytest.mark.asyncio
async def test_schedule_list_filters_config_owner_and_exposes_authority_marker(monkeypatch):
    import app.api.schedules as schedules_api
    from app.models.trigger import AgentTrigger

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    now = datetime.now(UTC)
    mine = AgentTrigger(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        name="mine",
        type="cron",
        config={"expr": "0 9 * * *", "created_by": str(owner_id), "authority_state": "owned"},
        reason="mine",
        is_enabled=True,
        fire_count=0,
        created_at=now,
    )
    foreign = AgentTrigger(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        name="foreign",
        type="cron",
        config={"expr": "0 10 * * *", "created_by": str(uuid4()), "authority_state": "owned"},
        reason="foreign",
        is_enabled=True,
        fire_count=0,
        created_at=now,
    )
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=tenant_id, department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    async def fake_access(*_args):
        return agent, "use"

    async def fake_filter(_db, _user, **kwargs):
        assert kwargs["owner_user_id_of"](mine) == owner_id
        assert kwargs["authority_state_of"](mine) == "owned"
        decision = SimpleNamespace(authority_source="resource_owner", operator_view=False)
        return [(mine, decision)]

    monkeypatch.setattr(schedules_api, "check_agent_access", fake_access)
    monkeypatch.setattr(schedules_api, "filter_authorized_resources", fake_filter, raising=False)

    payload = await schedules_api.list_schedules(
        agent_id=agent_id,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=_QueueDB([mine, foreign], [SimpleNamespace(id=owner_id, username="owner")]),
    )

    assert [row.id for row in payload] == [mine.id]
    assert payload[0].authority_source == "resource_owner"
    assert payload[0].operator_view is False


@pytest.mark.asyncio
async def test_chat_history_filters_foreign_sessions_before_loading_messages(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    mine = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        user_id=owner_id,
        root_session_id=None,
        source_channel="web",
        external_conv_id=None,
        delivery_target_json={"channel": "web", "user_label": "Mine"},
        peer_agent_id=None,
    )
    foreign = SimpleNamespace(
        **{
            **mine.__dict__,
            "id": uuid4(),
            "user_id": uuid4(),
            "delivery_target_json": {"channel": "web", "user_label": "Foreign"},
        }
    )
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=tenant_id, department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    filter_calls = []

    async def fake_access(*_args):
        return agent, "use"

    async def fake_filter(_db, _user, **kwargs):
        filter_calls.append(kwargs)
        decision = SimpleNamespace(authority_source="resource_owner", operator_view=False)
        return [(mine, decision)] if mine in kwargs["resources"] else []

    async def fake_stats(_db, **_kwargs):
        return 1, datetime.now(UTC)

    async def fake_last(_db, **_kwargs):
        return "mine only"

    monkeypatch.setattr(activity_api, "check_agent_access", fake_access)
    monkeypatch.setattr(activity_api, "filter_authorized_resources", fake_filter)
    monkeypatch.setattr(activity_api, "_get_session_message_stats", fake_stats)
    monkeypatch.setattr(activity_api, "_get_last_session_message", fake_last)

    payload = await activity_api.list_conversations(
        agent_id=agent_id,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=_QueueDB([mine, foreign], []),
    )

    assert [row["conv_id"] for row in payload] == [str(mine.id)]
    assert payload[0]["authority_source"] == "resource_owner"
    assert len(filter_calls) == 2
    assert all(call["resource_kind"] == "chat_session" for call in filter_calls)


def test_office_router_exposes_preview_without_legacy_capability_token_routes():
    import app.api.office as office_api

    route_paths = {route.path for route in office_api.router.routes}

    assert "/agents/{agent_id}/office/preview" in route_paths
    assert "/agents/{agent_id}/office/artifacts/{artifact_id}/preview" in route_paths
    assert "/agents/{agent_id}/office/editor-config" not in route_paths
    assert "/agents/{agent_id}/office/callback" not in route_paths
    assert "/agents/{agent_id}/office/force-save" not in route_paths
    assert "/agents/{agent_id}/office/download" not in route_paths


@pytest.mark.asyncio
async def test_office_preview_authorizes_resource_before_rendering(monkeypatch, tmp_path):
    import app.api.office as office_api
    from app.services.workspace_resource_authority import WorkspaceAuthorityError

    agent_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id, display_name="Member")
    target = tmp_path / str(agent_id) / "workspace" / "foreign.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"foreign")
    monkeypatch.setattr(
        office_api,
        "settings",
        SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), OFFICECLI_PREVIEW_MAX_BYTES=1024 * 1024),
    )

    async def fake_access(*_args):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_authorize(*_args, **_kwargs):
        raise WorkspaceAuthorityError("workspace_resource_forbidden", "foreign resource")

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)
    monkeypatch.setattr(office_api, "authorize_workspace_path", fake_authorize, raising=False)

    with pytest.raises(Exception) as exc_info:
        await office_api.preview_office_document(
            agent_id=agent_id,
            path="workspace/foreign.docx",
            operator_view=False,
            operator_reason=None,
            current_user=user,
            db=SimpleNamespace(),
        )

    assert getattr(exc_info.value, "status_code", None) == 403
    assert "workspace_resource_forbidden" in str(getattr(exc_info.value, "detail", ""))


@pytest.mark.asyncio
async def test_autonomy_overview_filters_foreign_trigger_resources(monkeypatch):
    from app.core.execution_context import ExecutionPrincipal
    from app.services import autonomy_overview

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    mine = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        type="cron",
        config={"created_by": str(owner_id), "authority_state": "owned", "expr": "0 9 * * *"},
        created_at=datetime.now(UTC),
        is_enabled=True,
        fire_count=0,
        last_fired_at=None,
        reason="mine",
        name="mine",
    )
    foreign = SimpleNamespace(
        **{
            **mine.__dict__,
            "id": uuid4(),
            "config": {"created_by": str(uuid4()), "authority_state": "owned", "expr": "0 10 * * *"},
            "name": "foreign",
        }
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    user = SimpleNamespace(id=owner_id, tenant_id=tenant_id, department_id=None)

    async def fake_attempts(*_args, **_kwargs):
        return []

    async def fake_filter(_db, _user, **kwargs):
        assert kwargs["triggers"] == [mine, foreign]
        decision = SimpleNamespace(authority_source="resource_owner", operator_view=False)
        return [(mine, decision)]

    monkeypatch.setattr(autonomy_overview, "_query_agent_runtime_tasks", fake_attempts)
    monkeypatch.setattr(autonomy_overview, "filter_authorized_triggers", fake_filter)

    payload = await autonomy_overview.build_agent_autonomy_overview(
        db=_QueueDB([mine, foreign]),
        agent=agent,
        principal=ExecutionPrincipal(
            tenant_id=tenant_id,
            source_agent_id=agent_id,
            requester_user_id=owner_id,
            origin="test",
        ),
        resource_user=user,
        agent_access=(agent, "use"),
    )

    assert payload["totals"]["triggers"] == 1
    assert [trigger["display_title"] for trigger in payload["triggers"]] == ["mine"]


@pytest.mark.asyncio
async def test_agent_use_cannot_bypass_resource_reads_through_raw_system_paths(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="member")
    system_file = tmp_path / str(agent_id) / "runtime_artifacts" / "triggers" / "foreign.json"
    system_file.parent.mkdir(parents=True)
    system_file.write_text('{"final_reply":"foreign"}', encoding="utf-8")
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_access(*_args):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)

    with pytest.raises(Exception) as exc_info:
        await files_api.read_file(
            agent_id=agent_id,
            path="runtime_artifacts/triggers/foreign.json",
            operator_view=False,
            operator_reason=None,
            current_user=user,
            db=SimpleNamespace(),
        )

    assert getattr(exc_info.value, "status_code", None) == 403
