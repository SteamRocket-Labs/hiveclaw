"""Phase 1 contract: Plan Mode as a first-class typed runtime state.

These tests define the target behaviour for promoting Plan Mode out of the
untyped ``SessionContext.metadata["plan_mode"]`` dict into a typed
:class:`PlanModeState`. The legacy dict shape MUST stay byte-compatible because
it is still consumed by the interactive ContextVar mirror, the ``exit_plan_mode``
tool, the prompt suffix, the frontend plan card, and existing tests.

Invariant under test (paradigm-convergence doc §6.1): the typed state is the
source of truth for the legacy metadata mirror, which must stay byte-compatible.
(Per-round reminder bookkeeping moved off this state entirely — the scheduler
in ``kernel/reminder_scheduler.py`` owns those clocks since T-G1.)
"""

from __future__ import annotations

from app.runtime.session import PlanModeState, SessionContext


def test_plan_mode_state_defaults_to_inactive():
    state = PlanModeState()
    assert state.active is False
    assert state.plan_id is None
    assert state.intent_type is None
    assert state.plan_file_path is None


def test_session_context_has_typed_plan_mode_field_inactive_by_default():
    ctx = SessionContext()
    assert isinstance(ctx.plan_mode, PlanModeState)
    assert ctx.plan_mode.active is False
    # Each SessionContext gets its own PlanModeState, not a shared instance.
    other = SessionContext()
    assert ctx.plan_mode is not other.plan_mode


def test_to_metadata_matches_legacy_dict_shape():
    state = PlanModeState(
        active=True,
        original_request="帮我完整调研这个行业",
        intent_type="in_session_execution",
        action_kind="start_long_task",
        tool_name="start_long_task",
        reason="explicit_request",
        handoff_target="long_task",
    )
    data = state.to_metadata()
    assert data == {
        "active": True,
        "original_request": "帮我完整调研这个行业",
        "intent_type": "in_session_execution",
        "action_kind": "start_long_task",
        "tool_name": "start_long_task",
        "reason": "explicit_request",
        "handoff_target": "long_task",
    }


def test_runtime_only_fields_never_leak_into_metadata_mirror():
    # Reminder bookkeeping no longer lives on this state (T-G1 moved it to the
    # scheduler); the mirror must stay free of any such runtime-only keys.
    # (plan_file_path DOES belong in the mirror — see the next test.)
    state = PlanModeState(active=True)
    data = state.to_metadata()
    assert "entered_round" not in data
    assert "reminded_full" not in data


def test_plan_file_path_round_trips_through_the_mirror():
    # Phase 4B: the interactive read-only gate reads plan_file_path off the
    # ContextVar mirror, so it must survive to_metadata/from_metadata.
    state = PlanModeState(active=True, plan_file_path="workspace/plans/s1.plan.md")
    data = state.to_metadata()
    assert data["plan_file_path"] == "workspace/plans/s1.plan.md"
    assert PlanModeState.from_metadata(data).plan_file_path == "workspace/plans/s1.plan.md"


def test_plan_id_absent_from_mirror_when_unset():
    # Ordinary explicit Plan Mode may not pre-arm a plan_id; the mirror must NOT
    # carry the key so exit_plan_mode falls into its "create new" branch.
    data = PlanModeState(active=True).to_metadata()
    assert "plan_id" not in data


def test_plan_id_round_trips_through_the_mirror_when_armed():
    # A system_plan_run launcher pre-arms Plan Mode with the draft's plan_id;
    # exit_plan_mode reads it off the ContextVar mirror to fill THAT draft instead
    # of creating a new one, so it must survive to_metadata.
    state = PlanModeState(active=True, plan_id="11111111-1111-1111-1111-111111111111")
    data = state.to_metadata()
    assert data["plan_id"] == "11111111-1111-1111-1111-111111111111"
    assert PlanModeState.from_metadata(data).plan_id == "11111111-1111-1111-1111-111111111111"


def test_is_interactive_plan_eligible_unifies_the_live_chat_boundary():
    # Phase 5 follow-up: one shared boundary for invoker + kernel. Real runtime
    # web-chat sessions use source="web"; unattended paths stay ineligible.
    from app.runtime.session import is_interactive_plan_eligible

    assert is_interactive_plan_eligible(SessionContext(source="web", channel="web")) is True
    assert is_interactive_plan_eligible(SessionContext(source="web_chat")) is True
    assert is_interactive_plan_eligible(SessionContext(source="chat")) is True
    assert is_interactive_plan_eligible(SessionContext(source="feishu", channel="feishu")) is True
    assert is_interactive_plan_eligible(SessionContext(source="wechat_personal", channel="wechat_personal")) is True
    assert is_interactive_plan_eligible(SessionContext(source="web", channel=None)) is True
    assert is_interactive_plan_eligible(SessionContext(source="trigger")) is False
    assert is_interactive_plan_eligible(SessionContext(source="heartbeat", channel=None)) is False
    assert is_interactive_plan_eligible(SessionContext(source="agent")) is False
    assert is_interactive_plan_eligible(None) is False


def test_is_unattended_plan_eligible_matches_only_trigger_and_heartbeat():
    """Unattended Plan Mode eligibility (path-unification §5.3 / cut ②): only
    multi-round daemon runs (trigger/heartbeat) qualify; live chat and one-shot
    surfaces do not (they use is_interactive_plan_eligible or static fail-closed)."""
    from app.runtime.session import is_unattended_plan_eligible

    assert is_unattended_plan_eligible(SessionContext(source="trigger")) is True
    assert is_unattended_plan_eligible(SessionContext(source="heartbeat", channel=None)) is True
    # live chat is NOT unattended — it has its own synchronous-confirmation path
    assert is_unattended_plan_eligible(SessionContext(source="web", channel="web")) is False
    assert is_unattended_plan_eligible(SessionContext(source="web_chat")) is False
    # delegation (source="agent") stays static fail-closed; there is no nested planner.
    assert is_unattended_plan_eligible(SessionContext(source="agent")) is False
    assert is_unattended_plan_eligible(None) is False


def test_interactive_and_unattended_eligibility_are_disjoint():
    """A session is never both — they map to different confirmation timings."""
    from app.runtime.session import is_interactive_plan_eligible, is_unattended_plan_eligible

    for src in ("web", "web_chat", "chat", "feishu", "trigger", "heartbeat", "agent", "runtime"):
        sc = SessionContext(source=src)
        assert not (is_interactive_plan_eligible(sc) and is_unattended_plan_eligible(sc))


def test_from_metadata_round_trips_core_fields():
    original = PlanModeState(
        active=True,
        original_request="req",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        reason="explicit_request",
        handoff_target="scheduled_trigger",
    )
    restored = PlanModeState.from_metadata(original.to_metadata())
    assert restored.active is True
    assert restored.original_request == "req"
    assert restored.intent_type == "autonomous_wake"
    assert restored.action_kind == "create_enabled_trigger"
    assert restored.tool_name == "set_trigger"
    assert restored.reason == "explicit_request"
    assert restored.handoff_target == "scheduled_trigger"


def test_from_metadata_handles_none_and_empty_safely():
    assert PlanModeState.from_metadata(None).active is False
    assert PlanModeState.from_metadata({}).active is False
    # A malformed/non-dict payload degrades to an inactive state, never raises.
    assert PlanModeState.from_metadata("not-a-dict").active is False  # type: ignore[arg-type]


def test_to_metadata_carries_action_artifact_when_present():
    """Optional action artifacts round-trip through the metadata mirror."""
    artifact = {"handoff": "continue_current_session", "args_hash": "args-hash"}
    state = PlanModeState(active=True, action_kind="start_long_task", action_artifact=artifact)
    data = state.to_metadata()
    assert data["action_artifact"] == artifact
    assert "action_artifact" not in PlanModeState(active=True).to_metadata()
    assert PlanModeState.from_metadata(data).action_artifact == artifact
    assert PlanModeState.from_metadata({"active": True}).action_artifact is None
