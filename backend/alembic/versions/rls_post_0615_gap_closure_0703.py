"""Close post-0615 RLS gaps on tenant-scoped session/collaboration tables.

Revision ID: rls_post_0615_gap_closure_0703
Revises: token_quota_hard_caps_0703
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "rls_post_0615_gap_closure_0703"
down_revision = "token_quota_hard_caps_0703"
branch_labels = None
depends_on = None


_TENANT_TABLES: tuple[str, ...] = (
    "agent_collaboration_groups",
    "agent_collaboration_group_members",
    "agent_session_goals",
    "agent_teams",
    "retired_trigger_focus_refs_0613",
)

_TEAM_CHILD_TABLES: tuple[str, ...] = (
    "agent_team_members",
    "agent_team_events",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _tenant_predicate(table: str) -> str:
    return f"""
        current_setting('app.current_tenant_id', true) = 'BYPASS'
        OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
        OR {table}.tenant_id IS NULL
    """


def _team_child_predicate(table: str) -> str:
    return f"""
        current_setting('app.current_tenant_id', true) = 'BYPASS'
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


def _enable_rls(table: str, predicate: str) -> None:
    policy_name = f"tenant_isolation_{table}"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
    op.execute(
        f"""
        CREATE POLICY {policy_name} ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
        """
    )


def upgrade() -> None:
    existing = _existing_tables()
    for table in _TENANT_TABLES:
        if table in existing:
            _enable_rls(table, _tenant_predicate(table))
    for table in _TEAM_CHILD_TABLES:
        if table in existing:
            _enable_rls(table, _team_child_predicate(table))


def downgrade() -> None:
    existing = _existing_tables()
    for table in reversed((*_TENANT_TABLES, *_TEAM_CHILD_TABLES)):
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
