"""Phase 1 contract: Plan Mode as a first-class typed runtime state.

These tests define the target behaviour for promoting Plan Mode out of the
untyped ``SessionContext.metadata["plan_mode"]`` dict into a typed
:class:`PlanModeState`. The legacy dict shape MUST stay byte-compatible because
it is still consumed by the interactive ContextVar mirror, the ``exit_plan_mode``
tool, the prompt suffix, the frontend plan card, and existing tests.

Invariant under test (paradigm-convergence doc §6.1): the typed state is the
source of truth for runtime injection bookkeeping (``reminded_full`` /
``entered_round``), which must NOT leak into the legacy metadata mirror to avoid
drift between the two representations.
"""

from __future__ import annotations

from app.runtime.session import PlanModeState, SessionContext


def test_plan_mode_state_defaults_to_inactive():
    state = PlanModeState()
    assert state.active is False
    assert state.plan_id is None
    assert state.intent_type is None
    assert state.entered_round == 0
    assert state.reminded_full is False
    assert state.plan_file_path is None


def test_session_context_has_typed_plan_mode_field_inactive_by_default():
    ctx = SessionContext()
    assert isinstance(ctx.plan_mode, PlanModeState)
    assert ctx.plan_mode.active is False
    # Each SessionContext gets its own PlanModeState, not a shared instance.
    other = SessionContext()
    assert ctx.plan_mode is not other.plan_mode


def test_to_metadata_matches_legacy_dict_shape_for_non_deep_research():
    state = PlanModeState(
        active=True,
        original_request="帮我完整调研这个行业",
        intent_type="long_task",
        action_kind="start_long_task",
        tool_name="start_long_task",
        reason="explicit_request",
        handoff_target="long_task",
    )
    data = state.to_metadata()
    assert data == {
        "active": True,
        "original_request": "帮我完整调研这个行业",
        "intent_type": "long_task",
        "action_kind": "start_long_task",
        "tool_name": "start_long_task",
        "reason": "explicit_request",
        "handoff_target": "long_task",
    }
    # Non-deep-research plans MUST NOT carry deep_research keys (byte-compat with
    # _activate_interactive_plan_mode's conditional update).
    assert "deep_research" not in data
    assert "deep_research_args" not in data


def test_to_metadata_includes_deep_research_payload_when_set():
    state = PlanModeState(
        active=True,
        original_request="使用 deepresearch做一个web3的全景报告",
        intent_type="long_task",
        action_kind="start_long_task",
        tool_name="start_long_task",
        reason="deep_research_request",
        handoff_target="deep_research",
        deep_research=True,
        deep_research_args={"question": "使用 deepresearch做一个web3的全景报告"},
    )
    data = state.to_metadata()
    assert data["handoff_target"] == "deep_research"
    assert data["deep_research"] is True
    assert data["deep_research_args"]["question"] == "使用 deepresearch做一个web3的全景报告"


def test_runtime_only_fields_never_leak_into_metadata_mirror():
    # reminded_full / entered_round are Phase 2 injection bookkeeping; they live
    # only on the typed state and must never reach the legacy dict (which feeds
    # the ContextVar + exit_plan_mode, neither of which knows about them).
    state = PlanModeState(active=True, entered_round=7, reminded_full=True, plan_file_path="x")
    data = state.to_metadata()
    assert "entered_round" not in data
    assert "reminded_full" not in data
    assert "plan_file_path" not in data


def test_from_metadata_round_trips_core_fields():
    original = PlanModeState(
        active=True,
        original_request="req",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        reason="explicit_request",
        handoff_target="objective_trigger",
        deep_research=False,
    )
    restored = PlanModeState.from_metadata(original.to_metadata())
    assert restored.active is True
    assert restored.original_request == "req"
    assert restored.intent_type == "autonomous_wake"
    assert restored.action_kind == "create_enabled_trigger"
    assert restored.tool_name == "set_trigger"
    assert restored.reason == "explicit_request"
    assert restored.handoff_target == "objective_trigger"
    assert restored.deep_research is False


def test_from_metadata_handles_none_and_empty_safely():
    assert PlanModeState.from_metadata(None).active is False
    assert PlanModeState.from_metadata({}).active is False
    # A malformed/non-dict payload degrades to an inactive state, never raises.
    assert PlanModeState.from_metadata("not-a-dict").active is False  # type: ignore[arg-type]
