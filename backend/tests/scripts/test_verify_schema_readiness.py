from __future__ import annotations

from dataclasses import replace

import pytest


def _healthy_state():
    from app.scripts.verify_schema_readiness import SchemaTableState

    return SchemaTableState(
        table_name="runtime_tasks",
        exists=True,
        rls_enabled=True,
        rls_forced=True,
        policy_count=1,
        has_tenant_id=True,
        tenant_id_not_null=True,
    )


def _healthy_audit_trigger():
    from app.scripts.verify_schema_readiness import SchemaTriggerState

    return SchemaTriggerState(
        trigger_name="trg_audit_logs_immutable",
        table_name="audit_logs",
        exists=True,
        enabled=True,
        is_row=True,
        is_before=True,
        handles_update=True,
        handles_delete=True,
        handles_truncate=False,
        function_name="reject_audit_evidence_mutation",
    )


def _healthy_audit_truncate_trigger():
    from app.scripts.verify_schema_readiness import SchemaTriggerState

    return SchemaTriggerState(
        trigger_name="trg_audit_logs_no_truncate",
        table_name="audit_logs",
        exists=True,
        enabled=True,
        is_row=False,
        is_before=True,
        handles_update=False,
        handles_delete=False,
        handles_truncate=True,
        function_name="reject_audit_evidence_mutation",
    )


def test_schema_readiness_accepts_matching_head_and_strict_catalog() -> None:
    from app.scripts.verify_schema_readiness import evaluate_schema_readiness

    report = evaluate_schema_readiness(
        expected_heads=("head_a",),
        actual_heads=("head_a",),
        expected_rls_tables=("runtime_tasks",),
        strict_tenant_tables=("runtime_tasks",),
        table_states={"runtime_tasks": _healthy_state()},
    )

    assert report.ready is True
    assert report.issues == ()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"exists": False}, "schema_table_missing"),
        ({"rls_enabled": False}, "rls_not_enabled"),
        ({"rls_forced": False}, "rls_not_forced"),
        ({"policy_count": 0}, "rls_policy_missing"),
        ({"has_tenant_id": False}, "strict_tenant_column_missing"),
        ({"tenant_id_not_null": False}, "strict_tenant_column_nullable"),
    ],
)
def test_schema_readiness_fails_closed_on_catalog_drift(
    mutation: dict[str, object],
    expected_code: str,
) -> None:
    from app.scripts.verify_schema_readiness import evaluate_schema_readiness

    state = replace(_healthy_state(), **mutation)
    report = evaluate_schema_readiness(
        expected_heads=("head_a",),
        actual_heads=("head_a",),
        expected_rls_tables=("runtime_tasks",),
        strict_tenant_tables=("runtime_tasks",),
        table_states={"runtime_tasks": state},
    )

    assert report.ready is False
    assert expected_code in {issue.code for issue in report.issues}


def test_schema_readiness_fails_closed_on_alembic_head_drift() -> None:
    from app.scripts.verify_schema_readiness import evaluate_schema_readiness

    report = evaluate_schema_readiness(
        expected_heads=("head_b",),
        actual_heads=("head_a",),
        expected_rls_tables=("runtime_tasks",),
        strict_tenant_tables=("runtime_tasks",),
        table_states={"runtime_tasks": _healthy_state()},
    )

    assert report.ready is False
    assert report.issues[0].code == "alembic_head_mismatch"
    assert report.issues[0].retryable is True


@pytest.mark.parametrize(
    ("trigger_states", "mutation", "expected_code"),
    [
        ({}, {}, "schema_trigger_missing"),
        (None, {"enabled": False}, "schema_trigger_disabled"),
        (None, {"handles_update": False}, "schema_trigger_invalid"),
        (None, {"handles_delete": False}, "schema_trigger_invalid"),
        (None, {"table_name": "other_table"}, "schema_trigger_invalid"),
        (None, {"function_name": "other_function"}, "schema_trigger_invalid"),
    ],
)
def test_schema_readiness_fails_closed_on_required_audit_trigger_drift(
    trigger_states: dict[str, object] | None,
    mutation: dict[str, object],
    expected_code: str,
) -> None:
    from app.scripts.verify_schema_readiness import (
        SchemaTriggerRequirement,
        evaluate_schema_readiness,
    )

    states = trigger_states
    if states is None:
        states = {
            "trg_audit_logs_immutable": replace(_healthy_audit_trigger(), **mutation),
        }
    report = evaluate_schema_readiness(
        expected_heads=("head_a",),
        actual_heads=("head_a",),
        expected_rls_tables=("runtime_tasks",),
        strict_tenant_tables=("runtime_tasks",),
        table_states={"runtime_tasks": _healthy_state()},
        required_triggers={
            "trg_audit_logs_immutable": SchemaTriggerRequirement(
                table_name="audit_logs",
                function_name="reject_audit_evidence_mutation",
                is_row=True,
                handles_update=True,
                handles_delete=True,
            )
        },
        trigger_states=states,
    )

    assert report.ready is False
    assert expected_code in {issue.code for issue in report.issues}


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"enabled": False}, "schema_trigger_disabled"),
        ({"is_row": True}, "schema_trigger_invalid"),
        ({"is_before": False}, "schema_trigger_invalid"),
        ({"handles_truncate": False}, "schema_trigger_invalid"),
    ],
)
def test_schema_readiness_fails_closed_on_audit_truncate_guard_drift(
    mutation: dict[str, object],
    expected_code: str,
) -> None:
    from app.scripts.verify_schema_readiness import (
        SchemaTriggerRequirement,
        evaluate_schema_readiness,
    )

    report = evaluate_schema_readiness(
        expected_heads=("head_a",),
        actual_heads=("head_a",),
        expected_rls_tables=("runtime_tasks",),
        strict_tenant_tables=("runtime_tasks",),
        table_states={"runtime_tasks": _healthy_state()},
        required_triggers={
            "trg_audit_logs_no_truncate": SchemaTriggerRequirement(
                table_name="audit_logs",
                function_name="reject_audit_evidence_mutation",
                is_row=False,
                handles_truncate=True,
            )
        },
        trigger_states={
            "trg_audit_logs_no_truncate": replace(
                _healthy_audit_truncate_trigger(),
                **mutation,
            )
        },
    )

    assert report.ready is False
    assert expected_code in {issue.code for issue in report.issues}
