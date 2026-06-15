from __future__ import annotations

from app.services.decision_trace import (
    DecisionTraceStore,
    decision_id_from_ref,
    extract_decision_id_from_text,
    normalize_decision_ref,
)


def test_feedback_links_back_to_decision_trace() -> None:
    store = DecisionTraceStore()
    decision = store.record_decision(
        action="external_reply",
        chosen="prepared two draft options and asked owner",
        reasoning="Customer escalation was urgent but reply was external-visible.",
        alternatives_considered=["reply directly", "ignore until tomorrow"],
        situational_factors=["external_visible", "owner_unavailable"],
        charter_zone="confirm_first",
        preflight={"representativeness": "high"},
        sensitivity="PL2_pii",
    )

    feedback = store.record_feedback(
        decision_id=decision.id,
        reaction="approved",
        polarity="positive",
        source="direct_owner",
        rationale_from_owner="Draft-first was correct.",
    )

    assert feedback.refs == f"decision/{decision.id}"
    assert store.feedback_for_decision(decision.id) == [feedback]


def test_unclear_feedback_does_not_create_calibration_candidate() -> None:
    store = DecisionTraceStore()
    decision = store.record_decision(
        action="schedule_change",
        chosen="prepared summary only",
        reasoning="Calendar change affects owner availability.",
        alternatives_considered=["change calendar directly"],
        situational_factors=["owner_busy"],
        charter_zone="confirm_first",
        preflight={},
        sensitivity="PL1_public",
    )
    store.record_feedback(decision_id=decision.id, reaction="unclear", polarity="neutral", source="direct_owner")

    assert store.calibration_candidates() == []


def test_decision_trace_store_persists_decisions_and_feedback(tmp_path) -> None:
    trace_path = tmp_path / "decision_traces.jsonl"
    first = DecisionTraceStore(path=trace_path)
    decision = first.record_decision(
        action="external_reply",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "refuse"],
        situational_factors=["vendor"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL2_pii",
    )

    second = DecisionTraceStore(path=trace_path)
    assert [item.id for item in second.decisions()] == [decision.id]
    second.record_feedback(
        decision_id=decision.id,
        reaction="too cautious",
        polarity="negative",
        source="direct_owner",
    )

    third = DecisionTraceStore(path=trace_path)
    assert third.feedback_for_decision(decision.id)[0].reaction == "too cautious"
    assert third.calibration_candidates() == [
        {
            "decision_id": decision.id,
            "action": "external_reply",
            "reaction": "too cautious",
            "charter_zone": "confirm_first",
        }
    ]


def test_decision_trace_persists_session_linkback_keys(tmp_path) -> None:
    trace_path = tmp_path / "decision_traces.jsonl"
    first = DecisionTraceStore(path=trace_path)
    decision = first.record_decision(
        action="send_feishu_message",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "ask"],
        situational_factors=["external_visible"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
        tenant_id="tenant-1",
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        message_id="message-1",
        tool_name="send_feishu_message",
        checkpoint_id="checkpoint-1",
    )

    second = DecisionTraceStore(path=trace_path)

    reloaded = second.get_decision(decision.id)
    assert reloaded.session_id == "session-1"
    assert reloaded.tenant_id == "tenant-1"
    assert reloaded.tool_name == "send_feishu_message"
    assert reloaded.checkpoint_id == "checkpoint-1"
    assert second.decisions_for_session("session-1", tenant_id="tenant-1") == [reloaded]
    assert second.decisions_for_session("session-1", tenant_id="other-tenant") == []


def test_decision_ref_helpers_normalize_and_extract() -> None:
    assert normalize_decision_ref("abc-123") == "decision/abc-123"
    assert normalize_decision_ref("decision/abc-123") == "decision/abc-123"
    assert decision_id_from_ref("decision/abc-123") == "abc-123"
    assert extract_decision_id_from_text("[Preflight:ask] decision=decision/abc-123") == "abc-123"
