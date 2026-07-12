"""Close bootstrap and upgrade RLS coverage for late tenant table families.

Revision ID: rls_complete_coverage_0712
Revises: mcp_metadata_trust_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "rls_complete_coverage_0712"
down_revision = "mcp_metadata_trust_0712"
branch_labels = None
depends_on = None


_STANDARD_TENANT_TABLES: tuple[str, ...] = (
    "agent_teams",
    "agent_session_goals",
    "local_agent_channel_events",
    "local_agent_channel_messages",
    "local_agent_channel_sessions",
)

_STRICT_TENANT_TABLES: tuple[str, ...] = (
    "agent_collaboration_groups",
    "agent_collaboration_group_members",
    "ai_asset_usage_events",
    "local_agent_channels",
    "local_agent_channel_ws_tickets",
    "workspace_resource_manifests",
)

_PARENT_DERIVED_TABLES: tuple[str, ...] = (
    "agent_team_members",
    "agent_team_events",
)

_ALL_TABLES: tuple[str, ...] = (
    *_STANDARD_TENANT_TABLES,
    *_STRICT_TENANT_TABLES,
    *_PARENT_DERIVED_TABLES,
)


def _bypass() -> str:
    return "current_setting('app.current_tenant_id', true) = 'BYPASS'"


def _standard_predicate(table: str) -> str:
    return f"""
        {_bypass()}
        OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
        OR {table}.tenant_id IS NULL
    """


def _strict_predicate(table: str) -> str:
    return f"""
        {_bypass()}
        OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
    """


def _team_child_predicate(table: str) -> str:
    return f"""
        {_bypass()}
        OR EXISTS (
            SELECT 1
            FROM agent_teams
            WHERE agent_teams.id = {table}.team_id
              AND (
                  agent_teams.tenant_id::text = current_setting('app.current_tenant_id', true)
                  OR agent_teams.tenant_id IS NULL
              )
        )
    """


def _install_policy(table: str, predicate: str) -> None:
    policy = f"tenant_isolation_{table}"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute(
        f"""
        CREATE POLICY {policy} ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
        """
    )


def upgrade() -> None:
    existing = set(inspect(op.get_bind()).get_table_names())
    for table in _STANDARD_TENANT_TABLES:
        if table in existing:
            _install_policy(table, _standard_predicate(table))
    for table in _STRICT_TENANT_TABLES:
        if table in existing:
            _install_policy(table, _strict_predicate(table))
    for table in _PARENT_DERIVED_TABLES:
        if table in existing:
            _install_policy(table, _team_child_predicate(table))


def downgrade() -> None:
    # Secure downgrade: removing tenant isolation would turn a code rollback
    # into a cross-tenant data exposure. Older application versions are
    # compatible with these DB-level policies, so preserve ENABLE/FORCE and
    # the policies. A later upgrade is idempotent and recreates exact text.
    pass
