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
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(hooks_api, "check_agent_access", fake_access)
    monkeypatch.setattr(
        hooks_api.hook_registry,
        "describe_registrations",
        lambda: [{"event": "stop", "handler_name": "h", "key": "hook.stop", "matcher_spec": None}],
    )
    monkeypatch.setattr(
        hooks_api,
        "describe_hook_runtime_config",
        lambda: {"items": [{"key": "hook.stop", "enabled": False, "timeout_seconds": 1.0, "failure_policy": "block"}]},
    )

    result = await hooks_api.list_agent_hooks(agent_id=agent_id, current_user=user, db=db)

    assert result["events"] == ["stop"]
    assert result["registrations"][0]["runtime_config"] == {
        "key": "hook.stop",
        "enabled": False,
        "timeout_seconds": 1.0,
        "failure_policy": "block",
    }


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

    result = await hooks_api.update_agent_hook_runtime_config(
        agent_id=agent_id,
        hook_key="hook.stop",
        body=hooks_api.HookRuntimeConfigIn(enabled=False, timeout_seconds=2.0, failure_policy="continue"),
        current_user=user,
        db=db,
    )

    assert result == {"ok": True, "config": {"key": "hook.stop", "enabled": False}}
    assert captured == {
        "key": "hook.stop",
        "agent_id": agent_id,
        "enabled": False,
        "timeout_seconds": 2.0,
        "failure_policy": "continue",
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
        body=hooks_api.HookRuntimeConfigIn(enabled=False, timeout_seconds=2.0, failure_policy="continue"),
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
            "failure_policy": "continue",
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
