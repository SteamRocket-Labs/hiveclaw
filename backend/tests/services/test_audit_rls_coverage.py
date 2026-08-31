"""Tests for the RLS coverage classifier (functional core)."""

from __future__ import annotations

from app.scripts.audit_rls_coverage import RlsCoverageReport, TableRls, analyze_rls_coverage


def test_unprotected_when_rls_not_enabled() -> None:
    report = analyze_rls_coverage([TableRls("tasks", True, False, False)], app_role_is_owner=True)
    assert report.unprotected == ["tasks"]
    assert report.inert == [] and report.enforced == []


def test_inert_when_enabled_not_forced_and_owner() -> None:
    """An owner-connected app with ENABLE-but-not-FORCE is bypassed — the P0-2 case."""
    report = analyze_rls_coverage([TableRls("agents", True, True, False)], app_role_is_owner=True)
    assert report.inert == ["agents"]
    assert report.enforced == []


def test_enforced_when_forced() -> None:
    report = analyze_rls_coverage([TableRls("workflow_definitions", True, True, True)], app_role_is_owner=True)
    assert report.enforced == ["workflow_definitions"]


def test_enforced_when_app_role_not_owner() -> None:
    """If the app is not the table owner, plain ENABLE already binds it."""
    report = analyze_rls_coverage([TableRls("agents", True, True, False)], app_role_is_owner=False)
    assert report.enforced == ["agents"]
    assert report.inert == []


def test_non_tenant_table_ignored() -> None:
    report = analyze_rls_coverage([TableRls("alembic_version", False, False, False)], app_role_is_owner=True)
    assert report == RlsCoverageReport([], [], [])


def test_mixed_fleet_is_bucketed_and_sorted() -> None:
    report = analyze_rls_coverage(
        [
            TableRls("tasks", True, False, False),
            TableRls("chat_sessions", True, False, False),
            TableRls("agents", True, True, False),
            TableRls("coordination_leases", True, True, True),
            TableRls("non_tenant", False, False, False),
        ],
        app_role_is_owner=True,
    )
    assert report.unprotected == ["chat_sessions", "tasks"]  # sorted
    assert report.inert == ["agents"]
    assert report.enforced == ["coordination_leases"]


def test_bootstrap_tenant_tables_are_all_forced_for_owner_runtime() -> None:
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, RLS_TENANT_TABLES

    missing = sorted(set(RLS_TENANT_TABLES) - set(RLS_FORCED_TENANT_TABLES))

    assert missing == []


def test_force_all_tenant_rls_migration_covers_bootstrap_force_tables() -> None:
    import importlib.util
    from pathlib import Path

    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES

    def _load_migration_module(filename: str):
        path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / filename
        spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    force_all_module = _load_migration_module("force_all_tenant_rls_0615.py")
    remaining_module = _load_migration_module("rls_remaining_global_and_derived_tables_0703.py")
    runtime_budget_module = _load_migration_module("runtime_budget_control_plane_0704.py")
    personal_knowledge_module = _load_migration_module("personal_knowledge_core_0707.py")
    external_capability_module = _load_migration_module("external_capability_rls_0709.py")
    ai_asset_module = _load_migration_module("ai_asset_control_plane_0710.py")
    personal_kb_local_module = _load_migration_module("personal_kb_local_receipts_0710.py")
    hr_creation_module = _load_migration_module("hr_creation_drafts_0710.py")
    hr_provisioning_module = _load_migration_module("hr_provisioning_steps_0711.py")
    workflow_confirmation_module = _load_migration_module("workflow_confirmation_0710.py")
    runtime_notification_module = _load_migration_module("runtime_notification_outbox_0710.py")
    channel_ingress_module = _load_migration_module("channel_ingress_inbox_0711.py")
    external_principal_module = _load_migration_module("external_principals_0711.py")
    channel_delivery_module = _load_migration_module("channel_delivery_outbox_0711.py")
    workflow_promotion_module = _load_migration_module("workflow_promotion_proposals_0711.py")
    budget_transition_module = _load_migration_module("budget_transition_outbox_0711.py")
    complete_coverage_module = _load_migration_module("rls_complete_coverage_0712.py")
    tenant_null_module = _load_migration_module("tenant_null_semantics_0712.py")
    storage_lifecycle_module = _load_migration_module("storage_blob_lifecycle_0715.py")
    session_v2_module = _load_migration_module("session_v2_0716.py")
    runtime_root_module = _load_migration_module("runtime_root_ledger_0716.py")
    runtime_result_module = _load_migration_module("runtime_result_fanin_0717.py")
    company_knowledge_module = _load_migration_module("company_knowledge_closed_loop_0724.py")
    company_knowledge_runtime_module = _load_migration_module("company_knowledge_runtime_0724.py")
    company_ontology_runtime_module = _load_migration_module("company_ontology_runtime_0724.py")
    runtime_terminal_module = _load_migration_module("runtime_terminal_boundary_0831.py")
    migration_tables = (
        set(force_all_module._FORCE_TABLES)
        | set(remaining_module._ALL_TABLES)
        | set(runtime_budget_module._RUNTIME_BUDGET_TABLES)
        | set(personal_knowledge_module._KNOWLEDGE_TABLES)
        | set(external_capability_module._EXTERNAL_CAPABILITY_TABLES)
        | set(ai_asset_module._AI_ASSET_TABLES)
        | set(personal_kb_local_module._PERSONAL_KB_LOCAL_RLS_TABLES)
        | set(hr_creation_module._HR_CREATION_TABLES)
        | set(hr_provisioning_module._HR_PROVISIONING_TABLES)
        | set(workflow_confirmation_module._TABLES)
        | set(runtime_notification_module._RUNTIME_NOTIFICATION_OUTBOX_TABLES)
        | set(channel_ingress_module._CHANNEL_INGRESS_TABLES)
        | set(external_principal_module._EXTERNAL_PRINCIPAL_RLS_TABLES)
        | set(channel_delivery_module._CHANNEL_DELIVERY_OUTBOX_TABLES)
        | set(workflow_promotion_module._WORKFLOW_PROMOTION_TABLES)
        | set(budget_transition_module._BUDGET_TRANSITION_TABLES)
        | set(complete_coverage_module._ALL_TABLES)
        | set(tenant_null_module.TENANT_OWNED_TABLES)
        | set(tenant_null_module.PLATFORM_SHARED)
        | set(tenant_null_module.OPERATOR_NULLABLE)
        | set(storage_lifecycle_module._TABLES)
        | set(session_v2_module.SESSION_V2_TENANT_TABLES)
        | set(runtime_root_module.RUNTIME_ROOT_LEDGER_TABLES)
        | set(runtime_result_module.RUNTIME_RESULT_TABLES)
        | set(company_knowledge_module.TENANT_TABLES)
        | {company_knowledge_runtime_module.TABLE}
        | set(company_ontology_runtime_module._COMPANY_TENANT_TABLES)
        | {runtime_terminal_module._TABLE}
    )

    missing = sorted(set(RLS_FORCED_TENANT_TABLES) - migration_tables)

    assert missing == []
