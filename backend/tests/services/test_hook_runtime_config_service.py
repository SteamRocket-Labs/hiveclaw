from __future__ import annotations

import inspect
from uuid import uuid4


def test_hook_runtime_config_key_round_trips_and_applies_agent_scope() -> None:
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
        {"hook.stop": {"enabled": False, "timeout_seconds": 2.0, "failure_policy": "block"}},
    )

    config = describe_hook_runtime_config("hook.stop", agent_id=agent_id)
    assert applied == 1
    assert config["enabled"] is False
    assert config["timeout_seconds"] == 2.0
    assert config["failure_policy"] == "required"
    reset_hook_runtime_config()


def test_restart_rehydrates_migration_preview_for_operator_diff() -> None:
    from app.runtime.hooks import describe_hook_runtime_config, reset_hook_runtime_config
    from app.services.hook_runtime_config import apply_agent_hook_runtime_configs

    reset_hook_runtime_config()
    agent_id = uuid4()
    apply_agent_hook_runtime_configs(
        agent_id,
        {
            "hook.prompt": {
                "failure_policy": "inherit",
                "migration_preview": {
                    "legacy_failure_policy": "continue",
                    "effective_change": "registration_default",
                },
            }
        },
    )

    config = describe_hook_runtime_config("hook.prompt", agent_id=agent_id)
    assert config["failure_policy"] == "inherit"
    assert config["migration_preview"]["legacy_failure_policy"] == "continue"
    reset_hook_runtime_config()


def test_startup_does_not_swallow_required_hook_loader_failures() -> None:
    from app import main

    source = inspect.getsource(main.lifespan)
    boundary = source.split("# Required hooks are authority boundaries.", 1)[1].split("# Backfill reply_context", 1)[0]

    assert "await register_installed_plugin_hooks()" in boundary
    assert "await apply_all_persisted_hook_runtime_configs()" in boundary
    assert "try:" not in boundary
    assert "except" not in boundary
