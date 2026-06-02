"""Add MCP server records — stable first-class MCP integration identity.

Revision ID: add_mcp_server_records_0602
Revises: add_agent_work_ledgers_0601
Create Date: 2026-06-02

Introduces four tenant-scoped tables (mcp_servers, mcp_server_tools,
agent_mcp_server_assignments, agent_mcp_tool_overrides) that replace the legacy
pack-derived MCP grouping. Every table joins Hive's row-level security with a
tenant_isolation_* policy mirroring add_row_level_security.py, so isolation is
enforced at the DB layer and not only by query-side filtering. tenant_id is
mandatory on all four tables because an RLS policy can only filter on a column
that physically exists on the row. See
docs/agent-extension-surface-skill-mcp.md §8.3.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "add_mcp_server_records_0602"
down_revision: Union[str, None] = "add_agent_work_ledgers_0601"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RLS_TABLES = [
    "mcp_servers",
    "mcp_server_tools",
    "agent_mcp_server_assignments",
    "agent_mcp_tool_overrides",
]


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("mcp_servers"):
        op.create_table(
            "mcp_servers",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("server_key", sa.String(length=200), nullable=False),
            sa.Column("transport", sa.String(length=40), nullable=True),
            sa.Column("server_url", sa.String(length=500), nullable=True),
            sa.Column("registry_source", sa.String(length=40), nullable=False, server_default="manual"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="disabled"),
            sa.Column("auth_status", sa.String(length=30), nullable=False, server_default="none"),
            sa.Column("config_json", JSONB(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "server_key", name="uq_mcp_servers_tenant_key"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_mcp_servers_tenant_id ON mcp_servers (tenant_id)")

    if not _table_exists("mcp_server_tools"):
        op.create_table(
            "mcp_server_tools",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "mcp_server_id",
                UUID(as_uuid=True),
                sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tool_id", UUID(as_uuid=True), sa.ForeignKey("tools.id", ondelete="CASCADE"), nullable=True),
            sa.Column("mcp_tool_name", sa.String(length=200), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("schema_hash", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "mcp_server_id", "mcp_tool_name", name="uq_mcp_server_tools_tenant_server_tool"
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_mcp_server_tools_tenant_id ON mcp_server_tools (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mcp_server_tools_server_id ON mcp_server_tools (mcp_server_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mcp_server_tools_tool_id ON mcp_server_tools (tool_id)")

    if not _table_exists("agent_mcp_server_assignments"):
        op.create_table(
            "agent_mcp_server_assignments",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "mcp_server_id",
                UUID(as_uuid=True),
                sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("default_tool_mode", sa.String(length=20), nullable=False, server_default="auto"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "agent_id", "mcp_server_id", name="uq_agent_mcp_assignment_tenant_agent_server"
            ),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_mcp_assignments_tenant_id ON agent_mcp_server_assignments (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_mcp_assignments_agent_id ON agent_mcp_server_assignments (agent_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_mcp_assignments_server_id ON agent_mcp_server_assignments (mcp_server_id)"
    )

    if not _table_exists("agent_mcp_tool_overrides"):
        op.create_table(
            "agent_mcp_tool_overrides",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "mcp_server_id",
                UUID(as_uuid=True),
                sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tool_name", sa.String(length=200), nullable=False),
            sa.Column("mode", sa.String(length=20), nullable=False, server_default="auto"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id",
                "agent_id",
                "mcp_server_id",
                "tool_name",
                name="uq_agent_mcp_override_tenant_agent_server_tool",
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_mcp_overrides_tenant_id ON agent_mcp_tool_overrides (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_mcp_overrides_agent_id ON agent_mcp_tool_overrides (agent_id)")

    # Row-level security: mirror add_row_level_security.py so every new table is
    # tenant-isolated at the DB layer, not just by query-side filtering. The
    # 'BYPASS'/IS NULL branches are kept verbatim from the production-verified
    # template; tenant_id is NOT NULL here so the IS NULL branch never fires.
    for table in _RLS_TABLES:
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
    for table in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("agent_mcp_tool_overrides")
    op.drop_table("agent_mcp_server_assignments")
    op.drop_table("mcp_server_tools")
    op.drop_table("mcp_servers")
