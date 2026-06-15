"""Add plugin dependency graph edges.

Revision ID: add_plugin_dependency_edges_0615
Revises: drop_workflow_step_phase_0614
Create Date: 2026-06-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "add_plugin_dependency_edges_0615"
down_revision: Union[str, None] = "drop_workflow_step_phase_0614"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "plugin_dependency_edges"


def _table_exists(table: str) -> bool:
    from sqlalchemy import text

    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "installed_plugin_id",
                UUID(as_uuid=True),
                sa.ForeignKey("tenant_installed_plugins.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "dependency_plugin_id",
                UUID(as_uuid=True),
                sa.ForeignKey("tenant_installed_plugins.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("dependency_key", sa.String(length=200), nullable=False),
            sa.Column("dependency_version", sa.String(length=40), nullable=False),
            sa.Column("source_kind", sa.String(length=40), nullable=False, server_default="builtin"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id",
                "installed_plugin_id",
                "dependency_plugin_id",
                name="uq_plugin_dependency_edge",
            ),
        )

    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_id ON {_TABLE} (tenant_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_installed_plugin_id ON {_TABLE} (installed_plugin_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_dependency_plugin_id ON {_TABLE} (dependency_plugin_id)")

    op.execute("SELECT set_config('app.current_tenant_id', '', false)")
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation_{_TABLE} ON {_TABLE}
                USING (
                    current_setting('app.current_tenant_id', true) = 'BYPASS'
                    OR tenant_id::text = current_setting('app.current_tenant_id', true)
                    OR tenant_id IS NULL
                );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_TABLE}")
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)
