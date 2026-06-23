from __future__ import annotations

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
    assert config["failure_policy"] == "block"
    reset_hook_runtime_config()
