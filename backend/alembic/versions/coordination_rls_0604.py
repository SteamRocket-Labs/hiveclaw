"""Force RLS on coordination runtime tables.

Revision ID: coordination_rls_0604
Revises: add_workflow_tables_0604
Create Date: 2026-06-04
"""

from typing import Sequence, Union

from alembic import op

revision: str = "coordination_rls_0604"
down_revision: Union[str, None] = "add_workflow_tables_0604"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COORDINATION_TABLES = ("coordination_leases", "coordination_signals", "coordination_checkpoints")


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
    for table in _COORDINATION_TABLES:
        if not _table_exists(table):
            # Coordination tables are created by create_all/bootstrap (no
            # create-table migration exists for them). On a deployment where
            # the app has not started yet, skip — bootstrap's
            # apply_rls_policies covers them when the table appears.
            continue
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
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
    for table in _COORDINATION_TABLES:
        if not _table_exists(table):
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
