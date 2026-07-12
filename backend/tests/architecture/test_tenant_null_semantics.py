from __future__ import annotations


EXPECTED_PLATFORM_SHARED_TABLES = {
    "agent_templates",
    "identity_providers",
    "llm_models",
    "runtime_budget_policies",
    "skills",
    "tools",
    "workflow_definitions",
}

EXPECTED_OPERATOR_NULLABLE_TABLES = {"audit_logs", "users"}


def test_every_tenant_column_has_one_explicit_null_semantics_classification() -> None:
    from app.database import Base
    from app.db_bootstrap import (
        OPERATOR_NULLABLE_TENANT_TABLES,
        PLATFORM_SHARED_TENANT_PREDICATES,
        STRICT_TENANT_RLS_TABLES,
    )
    from app.models import import_all_models

    import_all_models()
    tenant_tables = {table.name for table in Base.metadata.tables.values() if "tenant_id" in table.c}
    shared = set(PLATFORM_SHARED_TENANT_PREDICATES)
    operator_nullable = set(OPERATOR_NULLABLE_TENANT_TABLES)
    strict = set(STRICT_TENANT_RLS_TABLES)

    assert shared == EXPECTED_PLATFORM_SHARED_TABLES
    assert operator_nullable == EXPECTED_OPERATOR_NULLABLE_TABLES
    assert not (shared & operator_nullable)
    assert not (shared & strict)
    assert not (operator_nullable & strict)
    assert tenant_tables == shared | operator_nullable | strict

    nullable_tenant_owned = sorted(
        table.name
        for table in Base.metadata.tables.values()
        if "tenant_id" in table.c and table.name in strict and table.c.tenant_id.nullable
    )
    assert nullable_tenant_owned == [], f"tenant-owned tenant_id columns must be NOT NULL: {nullable_tenant_owned}"


def test_platform_shared_rows_are_readable_but_never_tenant_writable() -> None:
    from app.db_bootstrap import PLATFORM_SHARED_TENANT_PREDICATES, _policy_predicates_for_table

    semantic_markers = {
        "tools": "type = 'builtin'",
        "skills": "is_builtin IS TRUE",
        "agent_templates": "is_builtin IS TRUE",
        "workflow_definitions": "visibility_scope = 'platform'",
        "runtime_budget_policies": "scope_type = 'platform_default'",
        "identity_providers": "tenant_id IS NULL",
        "llm_models": "tenant_id IS NULL",
    }
    assert set(PLATFORM_SHARED_TENANT_PREDICATES) == set(semantic_markers)

    for table, marker in semantic_markers.items():
        using, check = _policy_predicates_for_table(table)
        assert marker in using
        assert f"{table}.tenant_id IS NULL" in using
        assert f"{table}.tenant_id IS NULL" not in check
        assert f"{table}.tenant_id::text" in check


def test_no_generic_or_parent_derived_policy_treats_null_as_shared() -> None:
    from app.db_bootstrap import (
        OPERATOR_NULLABLE_TENANT_TABLES,
        PLATFORM_SHARED_TENANT_PREDICATES,
        RLS_FORCED_TENANT_TABLES,
        _policy_predicates_for_table,
        _standard_tenant_predicate,
    )

    assert "IS NULL" not in _standard_tenant_predicate("example")
    explicit_shared = set(PLATFORM_SHARED_TENANT_PREDICATES)
    for table in RLS_FORCED_TENANT_TABLES:
        using, check = _policy_predicates_for_table(table)
        if table not in explicit_shared:
            assert f"{table}.tenant_id IS NULL" not in using
        assert "tenant_id IS NULL" not in check or table not in (set(OPERATOR_NULLABLE_TENANT_TABLES) | explicit_shared)
