"""Add plugin-system install tables (Step 5).

tenant_installed_plugins / agent_plugin_assignments / plugin_hook_registrations
— tenant-scoped with RLS ENABLEd **and FORCEd** from day one (P0 gap B: the
production connection is the table owner, so ENABLE-only policies are inert).
Generalizes the MCPServer install primitive to any capability pack. Every
accessor goes through tenant_scoped_session.

Revision ID: add_installed_plugin_tables_0614
Revises: retire_trigger_focus_ref_0613
Create Date: 2026-06-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "add_installed_plugin_tables_0614"
down_revision: Union[str, None] = "retire_trigger_focus_ref_0613"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PLUGIN_TABLES = ("tenant_installed_plugins", "agent_plugin_assignments", "plugin_hook_registrations")


def _table_exists(table: str) -> bool:
    from sqlalchemy import text

    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("tenant_installed_plugins"):
        op.create_table(
            "tenant_installed_plugins",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("plugin_key", sa.String(length=200), nullable=False),
            sa.Column("version", sa.String(length=40), nullable=False, server_default="0.0.0"),
            sa.Column("source_kind", sa.String(length=40), nullable=False, server_default="builtin"),
            sa.Column("source_ref", sa.String(length=500), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="enabled"),
            sa.Column("config_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("lockfile_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "plugin_key", name="uq_installed_plugins_tenant_key"),
        )

    if not _table_exists("agent_plugin_assignments"):
        op.create_table(
            "agent_plugin_assignments",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "installed_plugin_id",
                UUID(as_uuid=True),
                sa.ForeignKey("tenant_installed_plugins.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "agent_id", "installed_plugin_id", name="uq_agent_plugin_assignment"
            ),
        )

    if not _table_exists("plugin_hook_registrations"):
        op.create_table(
            "plugin_hook_registrations",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "installed_plugin_id",
                UUID(as_uuid=True),
                sa.ForeignKey("tenant_installed_plugins.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("event", sa.String(length=60), nullable=False),
            sa.Column("handler", sa.String(length=120), nullable=False),
            sa.Column("matcher_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("mode", sa.String(length=20), nullable=False, server_default="observe"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "installed_plugin_id", "event", "handler", name="uq_plugin_hook_registration"
            ),
        )

    for table in _PLUGIN_TABLES:
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plugin_assignments_agent_id ON agent_plugin_assignments (agent_id)"
    )
    for table in ("agent_plugin_assignments", "plugin_hook_registrations"):
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_installed_plugin_id ON {table} (installed_plugin_id)")

    # RLS: ENABLE + FORCE + tenant policy (P0 gap B — owner connection also bound).
    op.execute("SELECT set_config('app.current_tenant_id', '', false)")
    for table in _PLUGIN_TABLES:
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
    for table in reversed(_PLUGIN_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        if _table_exists(table):
            op.drop_table(table)
