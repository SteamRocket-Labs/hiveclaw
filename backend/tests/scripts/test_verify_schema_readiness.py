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
