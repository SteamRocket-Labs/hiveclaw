from app.services.execution_receipts import build_execution_receipt, canonical_payload_hash


def test_canonical_payload_hash_is_stable_across_mapping_order():
    left = {"target": "agent-b", "messages": [{"role": "user", "content": "hello"}]}
    right = {"messages": [{"content": "hello", "role": "user"}], "target": "agent-b"}

    assert canonical_payload_hash(left) == canonical_payload_hash(right)


def test_execution_receipt_deduplicates_result_refs_without_losing_order():
    receipt = build_execution_receipt(
        request_hash="request-hash",
        capability_snapshot_hash="authority-hash",
        result_refs=["runtime-task://task-1", "session://session-1", "runtime-task://task-1"],
        status="completed",
        replay_key="delegation:task-1",
        trace_id="trace-1",
        span_id="remote-action:task-1",
    )

    assert receipt == {
        "schema": "hive.execution_receipt.v1",
        "request_hash": "request-hash",
        "capability_snapshot_hash": "authority-hash",
        "result_refs": ["runtime-task://task-1", "session://session-1"],
        "status": "completed",
        "replay_key": "delegation:task-1",
        "trace_id": "trace-1",
        "span_id": "remote-action:task-1",
    }
