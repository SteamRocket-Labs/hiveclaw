"""Pure-core tests for Plan Mode *intercept-then-create-plan* mapping (§9.2).

When a Plan-Mode-gated tool is blocked, the tool gate creates an awaiting
PlanRequest seeded from the tool's own arguments so the user has something
concrete to confirm. The *mapping* from tool args -> plan_json ``fill`` is pure
logic (no IO / no DB), so it lives in ``plan_mode_core`` and is unit tested here
input -> output with no fakes.

The design (§9.2) gives the canonical example: ``set_trigger`` carries
``name`` / ``reason`` / ``config`` / ``type`` which map onto ``plan_json``'s
``title`` / ``objective`` / ``motivation`` / ``wake_policy``.
"""

from __future__ import annotations

from app.services.plan_mode_core import (
    action_kind_to_intent_signature,
    tool_args_to_plan_fill,
)


# ---------------------------------------------------------------------------
# tool_args_to_plan_fill — set_trigger (the §9.2 worked example)
# ---------------------------------------------------------------------------


def test_set_trigger_args_map_to_wake_policy_fill():
    fill = tool_args_to_plan_fill(
        tool_name="set_trigger",
        action_kind="create_enabled_trigger",
        arguments={
            "name": "Daily news brief",
            "type": "cron",
            "config": {"expr": "0 9 * * 1-5", "timezone": "Asia/Shanghai"},
            "reason": "User asked for a recurring morning industry summary.",
        },
    )

    # title comes from the trigger name; objective/motivation from the reason.
    assert fill["title"] == "Daily news brief"
    assert "recurring morning industry summary" in fill["objective"]
    assert fill["motivation"] == "User asked for a recurring morning industry summary."
    # wake_policy mirrors the trigger config + type.
    assert fill["wake_policy"]["type"] == "cron"
    assert fill["wake_policy"]["expr"] == "0 9 * * 1-5"
    assert fill["wake_policy"]["timezone"] == "Asia/Shanghai"
    # required_capabilities records the tool that was intercepted.
    assert "set_trigger" in fill["required_capabilities"]


def test_set_trigger_interval_maps_minutes():
    fill = tool_args_to_plan_fill(
        tool_name="set_trigger",
        action_kind="create_enabled_trigger",
        arguments={
            "name": "Poll status",
            "type": "interval",
            "config": {"minutes": 30},
            "reason": "Watch the deployment.",
        },
    )

    assert fill["wake_policy"]["type"] == "interval"
    assert fill["wake_policy"]["minutes"] == 30


def test_delegation_args_map_to_objective_fill():
    fill = tool_args_to_plan_fill(
        tool_name="delegate_to_agent",
        action_kind="start_delegation",
        arguments={"agent_name": "Researcher", "message": "Do a deep dive on RWA."},
    )

    assert "Researcher" in fill["title"]
    assert "deep dive on RWA" in fill["objective"]
    # A non-recurring intent gets a no-op wake policy.
    assert fill["wake_policy"]["type"] == "none"


def test_long_task_args_map_to_objective_fill():
    fill = tool_args_to_plan_fill(
        tool_name="manage_tasks",
        action_kind="start_long_task",
        arguments={"action": "create", "title": "Sweep stale files", "description": "Remove old logs weekly."},
    )

    assert fill["title"] == "Sweep stale files"
    assert "Remove old logs weekly." in fill["objective"]


def test_unknown_args_still_yield_a_safe_nonempty_fill():
    """The skeleton requires non-empty title/objective; a sparse call must still
    yield a fill that validates (no schema failure from a missing objective)."""
    fill = tool_args_to_plan_fill(
        tool_name="set_trigger",
        action_kind="create_enabled_trigger",
        arguments={},
    )

    assert str(fill["title"]).strip()
    assert str(fill["objective"]).strip()


# ---------------------------------------------------------------------------
# action_kind_to_intent_signature — the idempotency key parts
# ---------------------------------------------------------------------------


def test_signature_is_stable_for_same_logical_call():
    intent_a, sig_a = action_kind_to_intent_signature(
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        arguments={"name": "x", "type": "cron", "config": {"expr": "0 9 * * *"}},
    )
    # Re-ordered dict keys must not change the signature.
    intent_b, sig_b = action_kind_to_intent_signature(
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        arguments={"config": {"expr": "0 9 * * *"}, "type": "cron", "name": "x"},
    )

    assert intent_a == intent_b == "autonomous_wake"
    assert sig_a == sig_b


def test_signature_differs_for_different_calls():
    _intent, sig_a = action_kind_to_intent_signature(
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        arguments={"name": "x", "type": "cron", "config": {"expr": "0 9 * * *"}},
    )
    _intent2, sig_b = action_kind_to_intent_signature(
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        arguments={"name": "y", "type": "cron", "config": {"expr": "0 10 * * *"}},
    )

    assert sig_a != sig_b
