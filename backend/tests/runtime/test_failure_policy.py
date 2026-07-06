from __future__ import annotations


def test_runtime_failure_policy_classifies_hook_blocks_as_user_required_stop() -> None:
    from app.runtime.failure_policy import build_runtime_failure_policy

    policy = build_runtime_failure_policy(
        failure_kind="hook_block",
        message="policy blocked send_email",
        side_effect_risk="external_action_blocked",
    )

    assert policy["schema"] == "hive.ccplus.runtime_failure_policy.v1"
    assert policy["retryable"] is False
    assert policy["requires_user"] is True
    assert policy["requires_reconciliation"] is False
    assert policy["safe_to_continue"] is False
    assert "policy blocked send_email" in policy["model_visible_summary"]


def test_runtime_failure_policy_classifies_reconciliation_blocks() -> None:
    from app.runtime.failure_policy import build_runtime_failure_policy

    policy = build_runtime_failure_policy(
        failure_kind="workflow_leaf_failure",
        message="external step was in flight when the worker crashed",
        requires_reconciliation=True,
    )

    assert policy["retryable"] is False
    assert policy["requires_user"] is True
    assert policy["requires_reconciliation"] is True
    assert policy["safe_to_continue"] is False
