from __future__ import annotations


DERIVED_TENANT_TABLES = {"agent_team_members", "agent_team_events"}
R022_DIRECT_TABLES = {
    "agent_teams",
    "agent_collaboration_groups",
    "agent_collaboration_group_members",
    "agent_session_goals",
    "ai_asset_usage_events",
    "local_agent_channels",
    "local_agent_channel_events",
    "local_agent_channel_messages",
    "local_agent_channel_sessions",
    "local_agent_channel_ws_tickets",
    "workspace_resource_manifests",
}


def test_every_current_tenant_column_table_is_forced_on_fresh_bootstrap() -> None:
    from app.database import Base
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES
    from app.models import import_all_models

    import_all_models()
    tenant_tables = {table.name for table in Base.metadata.tables.values() if "tenant_id" in table.c}

    assert tenant_tables <= set(RLS_FORCED_TENANT_TABLES), (
        "Every ORM table carrying tenant_id must be ENABLE+FORCE RLS on the "
        f"create_all bootstrap path; missing={sorted(tenant_tables - set(RLS_FORCED_TENANT_TABLES))}"
    )


def test_r022_direct_and_parent_derived_tables_have_nontrivial_bootstrap_policies() -> None:
    from app.db_bootstrap import (
        RLS_FORCED_TENANT_TABLES,
        STRICT_TENANT_RLS_TABLES,
        _policy_predicates_for_table,
    )

    forced = set(RLS_FORCED_TENANT_TABLES)
    assert R022_DIRECT_TABLES | DERIVED_TENANT_TABLES <= forced
    assert {
        "agent_collaboration_groups",
        "agent_collaboration_group_members",
        "ai_asset_usage_events",
        "local_agent_channels",
        "local_agent_channel_ws_tickets",
        "workspace_resource_manifests",
    } <= set(STRICT_TENANT_RLS_TABLES)

    for table in DERIVED_TENANT_TABLES:
        using, check = _policy_predicates_for_table(table)
        assert "agent_teams" in using
        assert f"{table}.team_id" in using
        assert "agent_teams" in check
        assert using.strip() != "true"
