from app.kernel.engine import _merge_trace_metadata_sink


def test_tool_decision_and_receipt_join_keys_project_into_invocation_span() -> None:
    span_metadata = {"status": "ok"}
    trace_metadata = {
        "tool_decision": {"decision_id": "decision-1", "outcome": "allow"},
        "decision_id": "decision-1",
        "input_hash": "input-hash",
        "policy_snapshot_hash": "policy-hash",
        "capability_snapshot_hash": "capability-hash",
        "idempotency_key": "tool-call:1",
        "authority_policy_snapshot": {"guard_policy": {"version": 4}},
        "preflight": {"outcome": "DO"},
    }

    _merge_trace_metadata_sink(span_metadata, trace_metadata)

    assert span_metadata["decision_id"] == "decision-1"
    assert span_metadata["tool_decision"]["outcome"] == "allow"
    assert span_metadata["input_hash"] == "input-hash"
    assert span_metadata["policy_snapshot_hash"] == "policy-hash"
    assert span_metadata["capability_snapshot_hash"] == "capability-hash"
    assert span_metadata["idempotency_key"] == "tool-call:1"
    assert span_metadata["authority_policy_snapshot"]["guard_policy"]["version"] == 4
