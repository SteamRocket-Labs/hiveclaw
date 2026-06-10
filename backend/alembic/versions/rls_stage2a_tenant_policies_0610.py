"""Stage-2a: ENABLE RLS + tenant policy on 21 tenant-scoped tables that carried
tenant_id but had NO RLS policy.

Goal-2 isolation gap (found by reflecting Base.metadata against
db_bootstrap.RLS_TENANT_TABLES): these tables already carry tenant_id, yet the
bootstrap RLS list only covered 16 tables, so these 21 had neither ENABLE nor a
policy. Under the stage-3 non-owner role flip they would NOT fail-closed — they
would stay fully readable cross-tenant (silent isolation leak, worse than a
crash). This migration is policy-only (no column add, no backfill — the column
already exists). ENABLE-only (not FORCE): it takes effect at the role flip,
identical to the 9 historic ENABLE tables. Mirrors db_bootstrap.RLS_TENANT_TABLES.

Revision ID: rls_stage2a_tenant_policies_0610
Revises: backfill_patch_only_columns_0609
Create Date: 2026-06-10
"""

from typing import Sequence, Union

from alembic import op

revision: str = "rls_stage2a_tenant_policies_0610"
down_revision: Union[str, None] = "backfill_patch_only_columns_0609"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STAGE2A_TABLES = (
    "agent_mcp_server_assignments",
    "agent_mcp_tool_overrides",
    "agent_plan_recommendations",
    "agent_plan_requests",
    "agent_templates",
    "agent_work_ledgers",
    "capability_policies",
    "charter_proposals",
    "departments",
    "enterprise_info",
    "guard_policies",
    "identity_providers",
    "invitation_codes",
    "mcp_server_tools",
    "mcp_servers",
    "resource_permissions",
    "security_audit_events",
    "sso_scan_sessions",
    "tenant_channel_configs",
    "tenant_settings",
    "tenant_tool_configs",
)


def _table_exists(table: str) -> bool:
    from sqlalchemy import text

    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', '', false)")
    for table in _STAGE2A_TABLES:
        if not _table_exists(table):
            # Created by create_all/bootstrap on a not-yet-started deployment;
            # bootstrap's apply_rls_policies covers it when the table appears.
            continue
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            DO $$ BEGIN
                CREATE POLICY tenant_isolation_{table} ON {table}
                    USING (
                        current_setting('app.current_tenant_id', true) = 'BYPASS'
                        OR tenant_id::text = current_setting('app.current_tenant_id', true)
                        OR tenant_id IS NULL
                    );
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
        """)


def downgrade() -> None:
    for table in _STAGE2A_TABLES:
        if not _table_exists(table):
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
