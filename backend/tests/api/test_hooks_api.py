from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_employee_runtime_health_projects_actionable_status_without_hook_internals(monkeypatch):
    import app.api.hooks as hooks_api

    agent_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member")
    db = object()

    async def fake_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(
        hooks_api,
        "_read_recent_hook_receipts",
        lambda *_args, **_kwargs: _async_value(
            [
                {
                    "id": "receipt-secret",
                    "hook_key": "memory.pre_compaction.t0_checkpoint",
                    "event": "pre_compaction",
                    "status": "error",
                    "failure_mode": "required",
                    "retryable": True,
                    "error": "DatabaseError: internal diagnostic",
                    "created_at": "2026-07-24T04:00:00+00:00",
                },
                {
                    "id": "receipt-advisory",
                    "hook_key": "memory.response_complete.fast_reflection",
                    "event": "response_complete",
                    "status": "timeout",
                    "failure_mode": "advisory",
                    "retryable": True,
                    "error": "TimeoutError",
                    "created_at": "2026-07-24T03:00:00+00:00",
                },
            ]
        ),
    )

    result = await hooks_api.get_agent_runtime_health(agent_id=agent_id, current_user=user, db=db)

    assert result == {
        "schema": "hive.agent.runtime_health.v1",
        "agent_id": str(agent_id),
        "status": "needs_attention",
        "interrupted_turns": 1,
        "observed_issues": 1,
        "retry_available": True,
        "last_issue_at": "2026-07-24T04:00:00+00:00",
    }
    serialized = repr(result)
    for internal_value in (
        "hook",
        "pre_compaction",
        "response_complete",
        "memory.",
        "DatabaseError",
        "TimeoutError",
        "receipt-secret",
    ):
        assert internal_value not in serialized


@pytest.mark.asyncio
async def test_raw_hook_diagnostics_require_platform_developer_role(monkeypatch):
    import app.api.hooks as hooks_api

    access_called = False

    async def fake_access(*_args, **_kwargs):
        nonlocal access_called
        access_called = True
        return SimpleNamespace(id=uuid4(), tenant_id=uuid4()), "manage"

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)

    with pytest.raises(HTTPException) as exc:
        await hooks_api.list_agent_hook_diagnostics(
            agent_id=uuid4(),
            current_user=SimpleNamespace(id=uuid4(), role="org_admin"),
            db=object(),
        )

    assert exc.value.status_code == 403
    assert access_called is False


@pytest.mark.asyncio
async def test_raw_hook_diagnostics_are_filtered_to_the_selected_agent_and_tenant(monkeypatch):
    import app.api.hooks as hooks_api

    agent_id = uuid4()
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="platform_admin")
    db = object()

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(
        hooks_api.hook_registry,
        "describe_registrations",
        lambda: [
            {
                "event": "stop",
                "handler_name": "internal_stop",
                "key": "memory.turn_stop.t0",
                "profile_name": None,
                "matcher_spec": None,
                "failure_mode": "required",
            },
            {
                "event": "pre_tool_use",
                "handler_name": "governed_hook_runner",
                "key": f"plugin:{tenant_id}:allowed:registration-1",
                "profile_name": "plugin:allowed:prompt",
                "matcher_spec": {
                    "tenant_ids": [str(tenant_id)],
                    "agent_ids": [str(agent_id)],
                },
                "failure_mode": "advisory",
            },
            {
                "event": "pre_tool_use",
                "handler_name": "other_tenant_handler",
                "key": f"plugin:{other_tenant_id}:private:registration-2",
                "profile_name": "plugin:private",
                "matcher_spec": {
                    "tenant_ids": [str(other_tenant_id)],
                    "agent_ids": [str(uuid4())],
                },
                "failure_mode": "advisory",
            },
        ],
    )
    monkeypatch.setattr(
        hooks_api.hook_registry,
        "describe_event_catalog",
        lambda: [
            {"event": "pre_tool_use", "category": "tool", "handler_count": 99},
            {"event": "stop", "category": "turn", "handler_count": 99},
        ],
    )
    monkeypatch.setattr(
        hooks_api,
        "_describe_runtime_config_for_agent",
        lambda _agent_id: {
            "items": [
                {
                    "key": f"plugin:{tenant_id}:allowed:registration-1",
                    "enabled": True,
                    "failure_policy": "inherit",
                }
            ]
        },
    )
    monkeypatch.setattr(hooks_api, "_read_recent_hook_receipts", lambda *_args, **_kwargs: _async_value([]))

    result = await hooks_api.list_agent_hook_diagnostics(agent_id=agent_id, current_user=user, db=db)

    assert result["schema"] == "hive.platform.runtime_hook_diagnostics.v1"
    assert result["registered_events"] == ["pre_tool_use", "stop"]
    assert [item["handler_name"] for item in result["registrations"]] == [
        "internal_stop",
        "governed_hook_runner",
    ]
    assert {item["event"]: item["handler_count"] for item in result["events"]} == {
        "pre_tool_use": 1,
        "stop": 1,
    }
    assert str(other_tenant_id) not in repr(result)
    assert "other_tenant_handler" not in repr(result)


@pytest.mark.asyncio
async def test_platform_developer_cannot_mutate_builtin_hook(monkeypatch):
    import app.api.hooks as hooks_api

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="platform_admin")

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "manage"

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(
        hooks_api.hook_registry,
        "describe_registrations",
        lambda: [
            {
                "event": "stop",
                "handler_name": "t0_turn_stop",
                "key": "memory.turn_stop.t0",
                "profile_name": None,
                "failure_mode": "required",
            }
        ],
    )

    with pytest.raises(HTTPException) as exc:
        await hooks_api.update_agent_extension_hook_runtime_config(
            agent_id=agent_id,
            hook_key="memory.turn_stop.t0",
            body=hooks_api.HookRuntimeConfigIn(enabled=False),
            current_user=user,
            db=object(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "Built-in runtime safeguards are immutable per employee"


@pytest.mark.asyncio
async def test_platform_developer_can_update_registered_extension_with_security_audit(monkeypatch):
    import app.api.hooks as hooks_api

    agent_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="platform_admin")
    db = SimpleNamespace(commit=_async_value)
    persisted = {}
    audited = {}
    configured = {}
    plugin_key = f"plugin:{tenant_id}:allowed:registration-1"

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_persist(db_arg, *, agent_id, key, config):
        persisted.update({"db": db_arg, "agent_id": agent_id, "key": key, "config": config})

    async def fake_audit(**kwargs):
        audited.update(kwargs)

    def fake_configure(**kwargs):
        configured.update(kwargs)
        return {"key": kwargs["key"], "enabled": kwargs["enabled"], "failure_policy": "advisory"}

    commit_calls = []

    async def fake_commit():
        commit_calls.append(True)

    db.commit = fake_commit
    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(
        hooks_api.hook_registry,
        "describe_registrations",
        lambda: [
            {
                "event": "pre_tool_use",
                "handler_name": "governed_hook_runner",
                "key": plugin_key,
                "profile_name": "plugin:allowed:prompt",
                "failure_mode": "advisory",
            }
        ],
    )
    monkeypatch.setattr(hooks_api, "_persist_agent_hook_runtime_config", fake_persist)
    monkeypatch.setattr(hooks_api, "_write_hook_runtime_change_audit", fake_audit)
    monkeypatch.setattr(hooks_api, "configure_hook_runtime", fake_configure)

    result = await hooks_api.update_agent_extension_hook_runtime_config(
        agent_id=agent_id,
        hook_key=plugin_key,
        body=hooks_api.HookRuntimeConfigIn(enabled=False, timeout_seconds=2.0, failure_policy="advisory"),
        current_user=user,
        db=db,
    )

    assert result["ok"] is True
    assert persisted["config"] == {
        "key": plugin_key,
        "enabled": False,
        "timeout_seconds": 2.0,
        "failure_policy": "advisory",
    }
    assert audited["actor_id"] == user.id
    assert audited["agent_id"] == agent_id
    assert audited["hook_key"] == plugin_key
    assert audited["config"] == persisted["config"]
    assert commit_calls == [True]
    assert configured == {
        "key": plugin_key,
        "agent_id": agent_id,
        "enabled": False,
        "timeout_seconds": 2.0,
        "failure_policy": "advisory",
    }


def test_hook_router_separates_employee_health_from_platform_diagnostics():
    from app.api.hooks import router

    paths = {route.path for route in router.routes}

    assert "/agents/{agent_id}/runtime-health" in paths
    assert "/admin/agents/{agent_id}/runtime-hooks" in paths
    assert "/admin/agents/{agent_id}/runtime-hooks/{hook_key}" in paths
    assert "/agents/{agent_id}/hooks" not in paths
    assert "/agents/{agent_id}/hooks/{hook_key}" not in paths
