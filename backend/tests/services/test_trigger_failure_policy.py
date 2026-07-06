from __future__ import annotations

from types import SimpleNamespace


def test_trigger_failure_policy_returns_runtime_failure_policy() -> None:
    from app.services.trigger_failure_policy import apply_trigger_failure_policy

    trigger = SimpleNamespace(config={})

    result = apply_trigger_failure_policy(trigger, error="preflight denied")

    policy = result["runtime_failure_policy"]
    assert policy["failure_kind"] == "trigger_preflight_skip"
    assert policy["retryable"] is True
    assert policy["safe_to_continue"] is True
    assert trigger.config["last_runtime_failure_policy"] == policy
