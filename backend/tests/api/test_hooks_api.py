from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_hooks_api_lists_registrations_with_runtime_config(monkeypatch):
    import app.api.hooks as hooks_api

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member")
    db = object()

    async def fake_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(hooks_api, "_read_agent_hook_runtime_configs", lambda *_args, **_kwargs: _async_value({}))
    monkeypatch.setattr(
        hooks_api.hook_registry,
        "describe_registrations",
        lambda: [{"event": "stop", "handler_name": "h", "key": "hook.stop", "matcher_spec": None}],
    )
    monkeypatch.setattr(
        hooks_api.hook_registry,
        "describe_event_catalog",
        lambda: [
            {
                "event": "pre_tool_use",
                "category": "tool",
                "handler_count": 0,
                "blocking_supported": True,
                "standard": True,
            },
            {
                "event": "stop",
                "category": "turn",
                "handler_count": 1,
                "blocking_supported": True,
                "standard": True,
            },
        ],
        raising=False,
    )
    monkeypatch.setattr(
        hooks_api,
        "_read_recent_hook_receipts",
        lambda *_args, **_kwargs: _async_value(
            [
                {
                    "id": "receipt-1",
                    "hook_key": "hook.stop",
                    "event": "stop",
                    "status": "error",
                    "failure_mode": "required",
                    "retryable": True,
                    "error": "TimeoutError",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        hooks_api,
        "describe_hook_runtime_config",
        lambda: {"items": [{"key": "hook.stop", "enabled": False, "timeout_seconds": 1.0, "failure_policy": "block"}]},
    )

    result = await hooks_api.list_agent_hooks(agent_id=agent_id, current_user=user, db=db)

    assert result["schema"] == "hive.ccplus.hooks_control_plane.v2"
    assert result["registered_events"] == ["stop"]
    assert result["events"] == [
        {
            "event": "pre_tool_use",
            "category": "tool",
            "handler_count": 0,
            "blocking_supported": True,
            "standard": True,
        },
        {
            "event": "stop",
            "category": "turn",
            "handler_count": 1,
            "blocking_supported": True,
            "standard": True,
        },
    ]
    assert result["registrations"][0]["runtime_config"] == {
        "key": "hook.stop",
        "enabled": False,
        "timeout_seconds": 1.0,
        "failure_policy": "block",
        "effective_failure_mode": "required",
    }
    assert result["recent_receipts"][0]["retryable"] is True


@pytest.mark.asyncio
async def test_hooks_api_updates_runtime_config_with_manage_access(monkeypatch):
    import app.api.hooks as hooks_api

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member")
    db = object()
    captured = {}

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "manage"

    def fake_configure_hook_runtime(**kwargs):
        captured.update(kwargs)
        return {"key": kwargs["key"], "enabled": kwargs["enabled"]}

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(hooks_api, "configure_hook_runtime", fake_configure_hook_runtime)
    monkeypatch.setattr(hooks_api, "_persist_agent_hook_runtime_config", lambda *_args, **_kwargs: _async_value(None))

    result = await hooks_api.update_agent_hook_runtime_config(
        agent_id=agent_id,
        hook_key="hook.stop",
        body=hooks_api.HookRuntimeConfigIn(enabled=False, timeout_seconds=2.0, failure_policy="advisory"),
        current_user=user,
        db=db,
    )

    assert result == {"ok": True, "config": {"key": "hook.stop", "enabled": False}}
    assert captured == {
        "key": "hook.stop",
        "agent_id": agent_id,
        "enabled": False,
        "timeout_seconds": 2.0,
        "failure_policy": "advisory",
    }


@pytest.mark.asyncio
async def test_hooks_api_persists_runtime_config_with_manage_access(monkeypatch):
    import app.api.hooks as hooks_api

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member")
    db = object()
    persisted = {}

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "manage"

    async def fake_persist(db_arg, *, agent_id, key, config):
        persisted.update({"db": db_arg, "agent_id": agent_id, "key": key, "config": config})

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(hooks_api, "_persist_agent_hook_runtime_config", fake_persist)

    result = await hooks_api.update_agent_hook_runtime_config(
        agent_id=agent_id,
        hook_key="hook.stop",
        body=hooks_api.HookRuntimeConfigIn(enabled=False, timeout_seconds=2.0, failure_policy="advisory"),
        current_user=user,
        db=db,
    )

    assert result["ok"] is True
    assert persisted == {
        "db": db,
        "agent_id": agent_id,
        "key": "hook.stop",
        "config": {
            "key": "hook.stop",
            "enabled": False,
            "timeout_seconds": 2.0,
            "failure_policy": "advisory",
        },
    }


@pytest.mark.asyncio
async def test_hooks_api_rejects_update_without_manage_access(monkeypatch):
    import app.api.hooks as hooks_api
    from fastapi import HTTPException

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=uuid4()), "use"

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)

    with pytest.raises(HTTPException) as exc:
        await hooks_api.update_agent_hook_runtime_config(
            agent_id=uuid4(),
            hook_key="hook.stop",
            body=hooks_api.HookRuntimeConfigIn(enabled=False),
            current_user=SimpleNamespace(id=uuid4(), role="member"),
            db=object(),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_hooks_api_does_not_silently_drop_durable_config_failure(monkeypatch):
    import app.api.hooks as hooks_api

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=uuid4()), "manage"

    async def broken_persist(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(hooks_api, "persist_agent_hook_runtime_config", broken_persist)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await hooks_api.update_agent_hook_runtime_config(
            agent_id=uuid4(),
            hook_key="hook.stop",
            body=hooks_api.HookRuntimeConfigIn(enabled=False),
            current_user=SimpleNamespace(id=uuid4(), role="org_admin"),
            db=object(),
        )


async def _async_value(value):
    return value
