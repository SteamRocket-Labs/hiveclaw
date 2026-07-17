from types import SimpleNamespace

from app.services.delegation_session_repair import classify_terminal_delegation_projection


def test_classifies_legacy_predispatch_failure_without_inventing_model_output() -> None:
    disposition = classify_terminal_delegation_projection(
        SimpleNamespace(
            status="failed",
            result_summary="Task could not be dispatched because the target agent runtime is unavailable.",
            metadata_json={"dispatch_failed": True},
        )
    )

    assert disposition.status == "failed"
    assert disposition.reason == "target_runtime_unavailable"
    assert disposition.summary == "Task could not be dispatched because the target agent runtime is unavailable."


def test_classifies_legacy_coordination_lease_skip_as_typed_block() -> None:
    disposition = classify_terminal_delegation_projection(
        SimpleNamespace(
            status="skipped",
            result_summary="Equivalent work holds the lease.",
            metadata_json={
                "coordination_publish_state": "blocked",
                "blocked_by_lease_id": "lease-1",
            },
        )
    )

    assert disposition.status == "blocked"
    assert disposition.reason == "blocked_by_coordination_lease"
    assert disposition.summary == "Equivalent work holds the lease."


def test_classifies_reconciliation_as_hold_not_false_failure_or_success() -> None:
    disposition = classify_terminal_delegation_projection(
        SimpleNamespace(
            status="needs_reconciliation",
            result_summary=None,
            metadata_json={"root_item_reason_code": "coordination_publish_failed"},
        )
    )

    assert disposition.status == "blocked"
    assert disposition.reason == "coordination_publish_failed"
    assert "reconciliation" in disposition.summary.lower()
