from __future__ import annotations

from app.services.decision_trace import DecisionTraceStore


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
