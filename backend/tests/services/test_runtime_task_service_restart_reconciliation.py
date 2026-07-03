from __future__ import annotations


def test_restart_reconciliation_metadata_allows_audited_subagent_restart_retry():
    from app.services.runtime_task_service import build_restart_reconciliation_metadata

    metadata = build_restart_reconciliation_metadata(
        {"subagent_type": "general-purpose"},
        task_type="subagent",
        task_id="run-1",
        blocker="non_idempotent_subagent_type",
        summary="needs human retry",
        trace_id="trace-1",
        session_id="session-1",
    )

    assert metadata["reconciliation_retry_allowed"] is True
    assert metadata["reconciliation_retry_contract"] == {
        "schema": "runtime_reconciliation_retry_contract.v1",
        "kind": "audited_subagent_restart_retry",
        "task_type": "subagent",
        "task_id": "run-1",
        "blocker": "non_idempotent_subagent_type",
        "requires_human_approval": True,
        "retry_mode": "restart_from_prompt",
        "side_effect_risk": "mutating",
    }


def test_restart_reconciliation_metadata_does_not_retry_unsafe_child_tool_frame():
    from app.services.runtime_task_service import build_restart_reconciliation_metadata

    metadata = build_restart_reconciliation_metadata(
        {"child_pending_tool_frame": {"tool_name": "write_file"}},
        task_type="subagent",
        task_id="run-1",
        blocker="child_pending_tool_frame_not_replay_safe",
        summary="tool still running",
    )

    assert metadata.get("reconciliation_retry_allowed") is not True
    assert "reconciliation_retry_contract" not in metadata
