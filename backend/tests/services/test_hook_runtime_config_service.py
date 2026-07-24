from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_hook_runtime_config_key_round_trips_and_applies_only_registered_extension_scope() -> None:
    from app.runtime.hooks import describe_hook_runtime_config, reset_hook_runtime_config
    from app.services.hook_runtime_config import (
        agent_hook_runtime_config_key,
        apply_agent_hook_runtime_configs,
        parse_agent_hook_runtime_config_key,
    )

    reset_hook_runtime_config()
    agent_id = uuid4()
    key = agent_hook_runtime_config_key(agent_id)

    assert parse_agent_hook_runtime_config_key(key) == agent_id

    applied = apply_agent_hook_runtime_configs(
        agent_id,
        {
            "memory.turn_stop.t0": {"enabled": False, "failure_policy": "advisory"},
            "plugin:tenant:approved:registration-1": {
                "enabled": False,
                "timeout_seconds": 2.0,
                "failure_policy": "block",
            },
        },
        mutable_keys={"plugin:tenant:approved:registration-1"},
    )

    builtin = describe_hook_runtime_config("memory.turn_stop.t0", agent_id=agent_id)
    config = describe_hook_runtime_config("plugin:tenant:approved:registration-1", agent_id=agent_id)
    assert applied == 1
    assert builtin["enabled"] is True
    assert builtin["failure_policy"] == "inherit"
    assert config["enabled"] is False
    assert config["timeout_seconds"] == 2.0
    assert config["failure_policy"] == "required"
    reset_hook_runtime_config()


def test_restart_rehydrates_extension_migration_preview_for_operator_diff() -> None:
    from app.runtime.hooks import describe_hook_runtime_config, reset_hook_runtime_config
    from app.services.hook_runtime_config import apply_agent_hook_runtime_configs

    reset_hook_runtime_config()
    agent_id = uuid4()
    key = "plugin:tenant:approved:registration-1"
    apply_agent_hook_runtime_configs(
        agent_id,
        {
            key: {
                "failure_policy": "inherit",
                "migration_preview": {
                    "legacy_failure_policy": "continue",
                    "effective_change": "registration_default",
                },
            }
        },
        mutable_keys={key},
    )

    config = describe_hook_runtime_config(key, agent_id=agent_id)
    assert config["failure_policy"] == "inherit"
    assert config["migration_preview"]["legacy_failure_policy"] == "continue"
    reset_hook_runtime_config()


def test_internal_and_unregistered_overrides_move_to_recoverable_retirement_history() -> None:
    from app.services.hook_runtime_config import (
        normalize_hook_runtime_configs,
        retire_disallowed_hook_runtime_overrides,
    )

    plugin_key = "plugin:tenant:approved:registration-1"
    original = {
        "hooks": {
            "memory.turn_stop.t0": {"enabled": False, "failure_policy": "advisory"},
            plugin_key: {"enabled": False, "timeout_seconds": 3.0},
            "plugin:tenant:removed:registration-2": {"enabled": False},
        },
        "operator_note": "preserve me",
    }

    retired, receipt = retire_disallowed_hook_runtime_overrides(original, mutable_keys={plugin_key})

    assert normalize_hook_runtime_configs(retired) == {plugin_key: {"enabled": False, "timeout_seconds": 3.0}}
    assert retired["operator_note"] == "preserve me"
    assert receipt == {
        "active_extension_overrides": 1,
        "retired_overrides": 2,
        "retired_keys": [
            "memory.turn_stop.t0",
            "plugin:tenant:removed:registration-2",
        ],
    }
    retirement = retired["retired_hook_runtime_overrides"]
    assert retirement["memory.turn_stop.t0"][0]["reason"] == "built_in_hook_immutable"
    assert retirement["memory.turn_stop.t0"][0]["config"] == {
        "enabled": False,
        "failure_policy": "advisory",
    }
    assert retirement["plugin:tenant:removed:registration-2"][0]["reason"] == "extension_not_registered"
    assert len(retirement["memory.turn_stop.t0"][0]["sha256"]) == 64

    rerun, rerun_receipt = retire_disallowed_hook_runtime_overrides(retired, mutable_keys={plugin_key})
    assert rerun == retired
    assert rerun_receipt["retired_overrides"] == 0
    assert len(rerun["retired_hook_runtime_overrides"]["memory.turn_stop.t0"]) == 1


def test_persisting_an_extension_keeps_retired_internal_history_recoverable() -> None:
    from app.services import hook_runtime_config

    source = inspect.getsource(hook_runtime_config.persist_agent_hook_runtime_config)

    assert "retired_hook_runtime_overrides" in source
    assert 'payload["hooks"]' in source
    assert "await db.commit()" not in source


@pytest.mark.asyncio
async def test_persisting_an_extension_serializes_first_write_and_locks_existing_row() -> None:
    from app.services import hook_runtime_config

    agent_id = uuid4()
    row = SimpleNamespace(
        value={
            "hooks": {
                "plugin:tenant:approved:registration-1": {
                    "enabled": True,
                }
            }
        }
    )
    statements: list[tuple[str, dict | None]] = []

    class Result:
        def scalar_one_or_none(self):
            return row

    class FakeDb:
        async def execute(self, statement, params=None):
            statements.append((str(statement), params))
            return Result()

        async def flush(self):
            return None

    await hook_runtime_config.persist_agent_hook_runtime_config(
        FakeDb(),
        agent_id=agent_id,
        key="plugin:tenant:approved:registration-2",
        config={"enabled": False},
    )

    assert "pg_advisory_xact_lock" in statements[0][0]
    assert statements[0][1] == {"lock_key": hook_runtime_config.agent_hook_runtime_config_key(agent_id)}
    assert "FOR UPDATE" in statements[1][0]


@pytest.mark.asyncio
async def test_startup_retires_internal_overrides_audits_and_applies_only_registered_extensions(monkeypatch) -> None:
    from app.services import hook_runtime_config

    agent_id = uuid4()
    plugin_key = "plugin:tenant:approved:registration-1"
    row = SimpleNamespace(
        key=hook_runtime_config.agent_hook_runtime_config_key(agent_id),
        value={
            "hooks": {
                "memory.turn_stop.t0": {"enabled": False},
                plugin_key: {"enabled": False, "failure_policy": "advisory"},
            }
        },
    )
    commits: list[bool] = []
    audit_calls: list[list[dict]] = []
    applied: list[tuple] = []
    statements: list[str] = []

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [row]

    class FakeDb:
        async def execute(self, statement):
            statements.append(str(statement))
            return Result()

        async def commit(self):
            commits.append(True)

    class AsyncContext:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return None

    db = FakeDb()
    monkeypatch.setattr(hook_runtime_config, "async_session", lambda: AsyncContext(db))
    monkeypatch.setattr(
        hook_runtime_config,
        "enter_rls_bypass",
        lambda db_arg, **_kwargs: AsyncContext(db_arg),
    )
    monkeypatch.setattr(hook_runtime_config, "registered_extension_hook_keys", lambda: {plugin_key})
    monkeypatch.setattr(
        hook_runtime_config,
        "apply_agent_hook_runtime_configs",
        lambda *args, **kwargs: applied.append((args, kwargs)) or 1,
    )
    monkeypatch.setattr(
        hook_runtime_config,
        "_audit_retired_overrides",
        lambda *, changes: _capture_async(audit_calls, changes),
    )

    result = await hook_runtime_config.apply_all_persisted_hook_runtime_configs()

    assert result == 1
    assert commits == [True]
    assert audit_calls == [
        [
            {
                "agent_id": str(agent_id),
                "retired_overrides": 1,
                "retired_keys": ["memory.turn_stop.t0"],
            }
        ]
    ]
    assert row.value["hooks"] == {plugin_key: {"enabled": False, "failure_policy": "advisory"}}
    assert row.value["retired_hook_runtime_overrides"]["memory.turn_stop.t0"][0]["config"] == {"enabled": False}
    assert applied[0][0][0] == agent_id
    assert applied[0][0][1] == {plugin_key: {"enabled": False, "failure_policy": "advisory"}}
    assert applied[0][1]["mutable_keys"] == {plugin_key}
    assert "FOR UPDATE" in statements[0]


async def _capture_async(target: list, value) -> None:
    target.append(value)


def test_startup_does_not_swallow_required_hook_loader_failures() -> None:
    from app import main

    source = inspect.getsource(main.lifespan)
    boundary = source.split("# Required plugin-hook registrations are authority boundaries.", 1)[1].split(
        "# Backfill reply_context",
        1,
    )[0]

    assert "await register_installed_plugin_hooks()" in boundary
    assert "await apply_all_persisted_hook_runtime_configs()" in boundary
    assert "try:" not in boundary
    assert "except" not in boundary
