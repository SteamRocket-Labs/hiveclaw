from __future__ import annotations

import json

import pytest


def test_activation_event_manifest_is_json_safe_and_references_query_candidate() -> None:
    from app.runtime.activation_events import ActivationEvent, ActivationFeedback

    event = ActivationEvent(
        event_type="tool_success",
        session_id="session-1",
        turn_id="turn-1",
        intent_id="intent-1",
        query_id="aq:123",
        candidate_id="tool_schema:read_file:v1/abcdef",
        candidate_ref={"candidate_id": "tool_schema:read_file:v1/abcdef", "kind": "tool_schema"},
        feedback=ActivationFeedback(
            signal="tool_success",
            outcome="accepted",
            credit=0.8,
            reason="tool call succeeded after candidate disclosure",
        ),
        created_at="2026-07-06T00:00:00Z",
        source="post_tool_use",
        metadata={"tool_name": "read_file"},
    )

    manifest = event.to_manifest()

    assert manifest["schema"] == "hive.ccplus.activation_event.v1"
    assert manifest["event_id"].startswith("ae:")
    assert manifest["feedback"]["schema"] == "hive.ccplus.activation_feedback.v1"
    assert manifest["feedback"]["credit"] == pytest.approx(0.8)
    json.dumps(manifest)
    assert ActivationEvent.from_manifest(manifest) == event


def test_activation_event_ref_is_stable_after_roundtrip() -> None:
    from app.runtime.activation_events import ActivationEvent, build_activation_event_ref

    event = ActivationEvent(
        event_type="candidate_suppressed",
        session_id="session-2",
        turn_id="turn-2",
        intent_id="intent-2",
        query_id="aq:456",
        candidate_id="agent_memory:t3:v1/abcdef",
        created_at="2026-07-06T00:00:01Z",
        source="activation_router",
        metadata={"reason": "acl_denied"},
    )

    roundtripped = ActivationEvent.from_manifest(event.to_manifest())

    assert roundtripped == event
    assert build_activation_event_ref(roundtripped) == build_activation_event_ref(event)
    assert build_activation_event_ref(event)["schema"] == "hive.ccplus.activation_event_ref.v1"


def test_activation_event_from_manifest_rejects_wrong_schema() -> None:
    from app.runtime.activation_events import ActivationEvent

    with pytest.raises(ValueError, match="activation event schema"):
        ActivationEvent.from_manifest({"schema": "wrong"})


def test_activation_events_are_control_sidecar_not_truth_surface() -> None:
    from app.runtime.activation_events import activation_event_truth_surface_policy

    policy = activation_event_truth_surface_policy()

    assert policy["schema"] == "hive.ccplus.activation_event_truth_surface_policy.v1"
    assert policy["durable_truth_mutation"] is False
    assert "memory/control/" in policy["allowed_sidecar_roots"]
    assert "memory/t0/" in policy["forbidden_truth_surfaces"]
    assert "memory/t2/" in policy["forbidden_truth_surfaces"]
    assert "memory/t3/" in policy["forbidden_truth_surfaces"]
