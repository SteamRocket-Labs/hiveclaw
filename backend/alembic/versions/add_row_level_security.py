"""Enable PostgreSQL Row-Level Security on all tenant-scoped tables.

Revision ID: add_row_level_security
Revises: add_config_revisions
"""
from alembic import op

revision = "add_row_level_security"
down_revision = "add_config_revisions"
branch_labels = None
depends_on = None

# Tables with tenant_id column that need RLS
_TENANT_TABLES = [
    "agents",
    "users",
    "llm_models",
    "skills",
    "tools",
    "plaza_posts",
    "org_departments",
    "org_members",
    "config_revisions",
]


def upgrade() -> None:
    # Ensure the session variable exists (prevents errors on first query)
    op.execute("SELECT set_config('app.current_tenant_id', '', false)")

    for table in _TENANT_TABLES:
        # Enable RLS (no-op if already enabled)
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

        # Policy: rows visible only when tenant_id matches session variable.
        # Empty session variable ('' or unset) matches nothing — safe default.
        # Platform admin bypass: when app.current_tenant_id = 'BYPASS', all rows visible.
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
    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
