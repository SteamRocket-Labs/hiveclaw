"""Unit tests for the Plan Mode gate *functional core* (pure, no DB / no IO).

The gate's decision logic (does this autonomous action need a confirmed plan?)
is a deterministic function of already-fetched facts: the action kind, and the
state of the referenced plan row / artifact exemption. Those pure helpers live
in ``app.services.plan_mode_core`` next to the rest of the Plan Mode core and
are unit tested here input -> output with no fakes (per ``docs/plan-mode-design.md``
§9.0 / §9.2).

The DB-touching shell that fetches the plan row and resolves the artifact
exemption is ``app.services.plan_mode_gate.PlanModeGate`` and is integration
tested separately with the project's hand-rolled async-session fakes.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_action_kinds_are_the_documented_set():
    from app.services.plan_mode_core import ACTION_KINDS

    assert set(ACTION_KINDS) == {
        "create_enabled_trigger",
        "enable_autonomous_wake",
        "start_long_task",
        "start_delegation",
        "activate_objective_wake",
        "start_workflow",
    }


@pytest.mark.parametrize(
    "action_kind,expected_intent",
    [
        ("create_enabled_trigger", "autonomous_wake"),
        ("enable_autonomous_wake", "autonomous_wake"),
        ("activate_objective_wake", "autonomous_wake"),
        ("start_long_task", "long_task"),
        ("start_delegation", "delegation"),
    ],
)
def test_intent_type_for_action_maps_action_kind_to_intent(action_kind, expected_intent):
    from app.services.plan_mode_core import intent_type_for_action

    assert intent_type_for_action(action_kind) == expected_intent


def test_intent_type_for_action_rejects_unknown_kind():
    from app.services.plan_mode_core import intent_type_for_action

    with pytest.raises(ValueError, match="action_kind"):
        intent_type_for_action("nuke_production")


# ---------------------------------------------------------------------------
# Cutover exemption extraction (§9.0)
# ---------------------------------------------------------------------------


def test_extract_plan_exempt_reason_reads_top_level_metadata():
    from app.services.plan_mode_core import extract_plan_exempt_reason

    artifact = {"metadata": {"plan_exempt_reason": "preexisting_before_cutover"}}
    assert extract_plan_exempt_reason(artifact) == "preexisting_before_cutover"


def test_extract_plan_exempt_reason_reads_metadata_json_alias():
    from app.services.plan_mode_core import extract_plan_exempt_reason

    artifact = {"metadata_json": {"plan_exempt_reason": "preexisting_before_cutover"}}
    assert extract_plan_exempt_reason(artifact) == "preexisting_before_cutover"


def test_extract_plan_exempt_reason_reads_config_metadata():
    from app.services.plan_mode_core import extract_plan_exempt_reason

    # Triggers carry governance metadata under config.metadata.
    artifact = {"config": {"metadata": {"plan_exempt_reason": "preexisting_before_cutover"}}}
    assert extract_plan_exempt_reason(artifact) == "preexisting_before_cutover"


def test_extract_plan_exempt_reason_none_when_absent():
    from app.services.plan_mode_core import extract_plan_exempt_reason

    assert extract_plan_exempt_reason({"metadata": {}}) is None
    assert extract_plan_exempt_reason({}) is None
    assert extract_plan_exempt_reason(None) is None


def test_extract_plan_exempt_reason_ignores_blank():
    from app.services.plan_mode_core import extract_plan_exempt_reason

    assert extract_plan_exempt_reason({"metadata": {"plan_exempt_reason": "   "}}) is None


# ---------------------------------------------------------------------------
# Confirmed-plan handoff validation (§9.0 step 1)
# ---------------------------------------------------------------------------


def test_validate_plan_handoff_ok_when_confirmed_and_version_hash_match():
    from app.services.plan_mode_core import validate_plan_handoff

    check = validate_plan_handoff(
        status="confirmed",
        stored_version=1,
        stored_hash="sha256:abc",
        submitted_version=1,
        submitted_hash="sha256:abc",
    )
    assert check.ok is True
    assert check.error_code is None


def test_validate_plan_handoff_rejects_unconfirmed_status():
    from app.services.plan_mode_core import validate_plan_handoff

    check = validate_plan_handoff(
        status="awaiting_confirmation",
        stored_version=1,
        stored_hash="sha256:abc",
        submitted_version=1,
        submitted_hash="sha256:abc",
    )
    assert check.ok is False
    assert check.error_code == "plan_not_confirmed"


def test_validate_plan_handoff_version_mismatch():
    from app.services.plan_mode_core import validate_plan_handoff

    check = validate_plan_handoff(
        status="confirmed",
        stored_version=2,
        stored_hash="sha256:abc",
        submitted_version=1,
        submitted_hash="sha256:abc",
    )
    assert check.ok is False
    assert check.error_code == "version_mismatch"


def test_validate_plan_handoff_hash_mismatch():
    from app.services.plan_mode_core import validate_plan_handoff

    check = validate_plan_handoff(
        status="confirmed",
        stored_version=1,
        stored_hash="sha256:abc",
        submitted_version=1,
        submitted_hash="sha256:tampered",
    )
    assert check.ok is False
    assert check.error_code == "hash_mismatch"


def test_validate_plan_handoff_requires_submitted_version_and_hash():
    """Execution-layer handoff must bind to the exact confirmed version/hash."""
    from app.services.plan_mode_core import validate_plan_handoff

    check = validate_plan_handoff(
        status="confirmed",
        stored_version=3,
        stored_hash="sha256:abc",
        submitted_version=None,
        submitted_hash=None,
    )
    assert check.ok is False
    assert check.error_code == "missing_plan_version_hash"


# ---------------------------------------------------------------------------
# needs_plan payload assembly (§9.2 contract — isomorphic with deep_research)
# ---------------------------------------------------------------------------


def test_build_needs_plan_payload_shape_matches_contract():
    from app.services.plan_mode_core import build_needs_plan_payload

    payload = build_needs_plan_payload(
        plan_id="11111111-1111-1111-1111-111111111111",
        plan_version=1,
        summary="Confirm the plan before creating this autonomous wake policy.",
        plan_preview={"title": "Daily brief", "intent_type": "autonomous_wake"},
    )

    assert payload["ok"] is False
    assert payload["status"] == "needs_plan"
    assert payload["plan_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["plan_version"] == 1
    assert payload["summary"]
    assert payload["plan_preview"] == {"title": "Daily brief", "intent_type": "autonomous_wake"}
    assert payload["next_action"]


def test_build_needs_plan_payload_omits_optional_fields_when_absent():
    from app.services.plan_mode_core import build_needs_plan_payload

    payload = build_needs_plan_payload(summary="Create and confirm a plan first.")

    assert payload["ok"] is False
    assert payload["status"] == "needs_plan"
    assert "plan_id" not in payload
    assert "plan_version" not in payload
    assert "plan_preview" not in payload
    assert payload["summary"] == "Create and confirm a plan first."
    assert payload["next_action"]


def test_build_needs_plan_payload_default_summary_and_next_action_present():
    from app.services.plan_mode_core import build_needs_plan_payload

    payload = build_needs_plan_payload()
    assert payload["summary"]
    assert payload["next_action"]


def test_classify_plan_mode_entry_user_decline_does_not_reenter_plan_mode():
    from app.services.plan_mode_core import classify_plan_mode_entry

    decision = classify_plan_mode_entry("不用计划模式，直接创建这个每天 12 点运行的定时任务")

    assert decision.mode == "declined"
    assert decision.reason == "user_declined_recommended_plan_mode"


def test_default_needs_plan_summary_is_the_payload_default():
    from app.services.plan_mode_core import build_needs_plan_payload, default_needs_plan_summary

    assert build_needs_plan_payload()["summary"] == default_needs_plan_summary()


# ---------------------------------------------------------------------------
# Backstop classification: objective auto-wake exemption (§9.0)
# ---------------------------------------------------------------------------


def test_objective_wake_exempt_reason_confirmed_plan_metadata():
    from app.services.plan_mode_core import objective_wake_exempt_reason

    reason = objective_wake_exempt_reason(
        objective_metadata={"plan_id": "11111111-1111-1111-1111-111111111111"},
        autonomy_class="explicit_user_request",
    )
    assert reason == "confirmed_plan"


def test_objective_wake_exempt_reason_tolerates_non_string_plan_id():
    from uuid import uuid4

    from app.services.plan_mode_core import objective_wake_exempt_reason

    reason = objective_wake_exempt_reason(
        objective_metadata={"plan_id": uuid4()},
        autonomy_class="explicit_user_request",
    )
    assert reason == "confirmed_plan"


def test_objective_wake_exempt_reason_explicit_cutover_exemption():
    from app.services.plan_mode_core import objective_wake_exempt_reason

    reason = objective_wake_exempt_reason(
        objective_metadata={"plan_exempt_reason": "preexisting_before_cutover"},
        autonomy_class="explicit_user_request",
    )
    assert reason == "preexisting_before_cutover"


def test_objective_wake_exempt_reason_platform_internal_self_evolution():
    """The recovery / self-evolution loop must keep auto-waking without a plan."""
    from app.services.plan_mode_core import objective_wake_exempt_reason

    reason = objective_wake_exempt_reason(
        objective_metadata={},
        autonomy_class="internal_self_improvement",
    )
    assert reason == "platform_internal"


def test_objective_wake_exempt_reason_confirmed_hr_blueprint():
    from app.services.plan_mode_core import objective_wake_exempt_reason

    reason = objective_wake_exempt_reason(
        objective_metadata={"autonomy_class": "confirmed_hr_blueprint"},
        autonomy_class="confirmed_hr_blueprint",
    )
    assert reason == "confirmed_hr_blueprint"


def test_objective_wake_exempt_reason_none_for_conversation_intent():
    """An active objective born from a conversation intent (no plan, not internal)
    is the bypass the backstop closes — it must NOT be auto-woken."""
    from app.services.plan_mode_core import objective_wake_exempt_reason

    assert (
        objective_wake_exempt_reason(
            objective_metadata={"autonomy_class": "explicit_user_request"},
            autonomy_class="explicit_user_request",
        )
        is None
    )
    assert objective_wake_exempt_reason(objective_metadata=None, autonomy_class=None) is None


def test_objective_wake_exempt_reason_blank_plan_id_is_not_exempt():
    from app.services.plan_mode_core import objective_wake_exempt_reason

    assert (
        objective_wake_exempt_reason(
            objective_metadata={"plan_id": "   "},
            autonomy_class="explicit_user_request",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Backstop classification: which triggers self-initiate autonomy (§9.0)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger_type", ["cron", "interval", "once", "poll"])
def test_trigger_is_autonomous_true_for_schedule_loop_types(trigger_type):
    from app.services.plan_mode_core import trigger_is_autonomous

    assert trigger_is_autonomous(trigger_type=trigger_type, trigger_class=None) is True
    assert trigger_is_autonomous(trigger_type=trigger_type, trigger_class="scheduled_job") is True


@pytest.mark.parametrize("trigger_type", ["on_message", "webhook"])
def test_trigger_is_autonomous_false_for_reactive_types(trigger_type):
    """Reactive triggers wait for an external event — design §9.0 treats them as
    cutover legacy, not the autonomy the backstop must gate."""
    from app.services.plan_mode_core import trigger_is_autonomous

    assert trigger_is_autonomous(trigger_type=trigger_type, trigger_class=None) is False


def test_trigger_is_autonomous_false_for_platform_internal_classes():
    from app.services.plan_mode_core import trigger_is_autonomous

    assert trigger_is_autonomous(trigger_type="cron", trigger_class="event_wait") is False
    assert trigger_is_autonomous(trigger_type="interval", trigger_class="system_maintenance") is False


def test_trigger_is_autonomous_false_for_objective_task_class():
    """objective_task is governed by its bound objective's own preflight gate /
    config.plan_id, so the type-level classifier must not double-gate it."""
    from app.services.plan_mode_core import trigger_is_autonomous

    assert trigger_is_autonomous(trigger_type="cron", trigger_class="objective_task") is False


def test_classify_plan_mode_entry_recommends_for_schedule_intent():
    from app.services.plan_mode_core import classify_plan_mode_entry

    decision = classify_plan_mode_entry("每天 9 点帮我整理新闻")

    assert decision.mode == "recommend"
    assert decision.intent_type == "autonomous_wake"
    assert decision.action_kind == "create_enabled_trigger"
    assert decision.tool_name == "set_trigger"


def test_classify_plan_mode_entry_long_task_text_does_not_auto_enter():
    """A (user correction): the agent's judgment must NOT trigger Plan Mode entry.
    Pure long-task wording no longer auto-activates — entry stays user-explicit
    (the agent SUGGESTS via prompt guidance; the user decides). The pre-LLM
    long-task regex auto-trigger is removed."""
    from app.services.plan_mode_core import classify_plan_mode_entry

    decision = classify_plan_mode_entry("完整调研这个行业并输出报告")

    assert decision.mode == "none"


def test_classify_plan_mode_entry_recommends_when_schedule_and_long_task_overlap():
    from app.services.plan_mode_core import classify_plan_mode_entry

    decision = classify_plan_mode_entry("每天调研这个行业并输出报告")

    assert decision.mode == "recommend"
    assert decision.intent_type == "autonomous_wake"
    assert decision.action_kind == "create_enabled_trigger"
    assert decision.tool_name == "set_trigger"


def test_classify_plan_mode_entry_explicit_frontend_selection_enters_plan_mode():
    from app.services.plan_mode_core import classify_plan_mode_entry

    decision = classify_plan_mode_entry("帮我做一件事", explicit=True)

    assert decision.mode == "explicit"
    assert decision.intent_type == "long_task"
    assert decision.action_kind == "start_long_task"
