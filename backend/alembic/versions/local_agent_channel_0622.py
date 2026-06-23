"""Add Local Agent Channel tables.

Revision ID: local_agent_channel_0622
Revises: executable_chat_active_run_unique_0622
Create Date: 2026-06-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "local_agent_channel_0622"
down_revision = "executable_chat_active_run_unique_0622"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table: str) -> bool:
    return _inspector().has_table(table)


def _enable_tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation_{table} ON {table}
                USING (
                    current_setting('app.current_tenant_id', true) = 'BYPASS'
                    OR tenant_id::text = current_setting('app.current_tenant_id', true)
                    OR tenant_id IS NULL
                );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )


def upgrade() -> None:
    if _table_exists("local_agent_bridge_connections"):
        op.alter_column("local_agent_bridge_connections", "agent_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_local_bridge_connections_user_status "
            "ON local_agent_bridge_connections (user_id, status)"
        )

    if not _table_exists("local_agent_channels"):
        op.create_table(
            "local_agent_channels",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("runtime_kind", sa.String(length=64), nullable=False, server_default="unknown"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="offline"),
            sa.Column("capabilities_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["connection_id"], ["local_agent_bridge_connections.id"]),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("connection_id", name="uq_local_agent_channels_connection_id"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channels_user_status ON local_agent_channels (owner_user_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channels_tenant_id ON local_agent_channels (tenant_id)")
    _enable_tenant_rls("local_agent_channels")

    if not _table_exists("local_agent_channel_sessions"):
        op.create_table(
            "local_agent_channel_sessions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("chat_session_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("local_session_id", sa.String(length=128), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="web"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], ["local_agent_channels.id"]),
            sa.ForeignKeyConstraint(["chat_session_id"], ["chat_sessions.id"]),
            sa.ForeignKeyConstraint(["connection_id"], ["local_agent_bridge_connections.id"]),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["source_agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_sessions_user_status ON local_agent_channel_sessions (owner_user_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_sessions_source_agent ON local_agent_channel_sessions (source_agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_sessions_chat_session ON local_agent_channel_sessions (chat_session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_sessions_tenant_id ON local_agent_channel_sessions (tenant_id)")
    _enable_tenant_rls("local_agent_channel_sessions")

    if not _table_exists("local_agent_channel_messages"):
        op.create_table(
            "local_agent_channel_messages",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("sender_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("direction", sa.String(length=32), nullable=False, server_default="hive_to_local"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("attachments_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("result", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["source_agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["sender_agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["sender_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["session_id"], ["local_agent_channel_sessions.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_messages_session_status ON local_agent_channel_messages (session_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_messages_user_status ON local_agent_channel_messages (owner_user_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_messages_source_agent ON local_agent_channel_messages (source_agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_messages_tenant_id ON local_agent_channel_messages (tenant_id)")
    _enable_tenant_rls("local_agent_channel_messages")

    if not _table_exists("local_agent_channel_events"):
        op.create_table(
            "local_agent_channel_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("direction", sa.String(length=32), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["message_id"], ["local_agent_channel_messages.id"]),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["source_agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["session_id"], ["local_agent_channel_sessions.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_events_session_created ON local_agent_channel_events (session_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_events_message ON local_agent_channel_events (message_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_events_tenant_id ON local_agent_channel_events (tenant_id)")
    _enable_tenant_rls("local_agent_channel_events")

    if not _table_exists("local_agent_channel_ws_tickets"):
        op.create_table(
            "local_agent_channel_ws_tickets",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("ticket_hash", sa.String(length=128), nullable=False),
            sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["connection_id"], ["local_agent_bridge_connections.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ticket_hash", name="uq_local_agent_channel_ws_ticket_hash"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_ws_tickets_connection ON local_agent_channel_ws_tickets (connection_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_ws_tickets_expires ON local_agent_channel_ws_tickets (expires_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_agent_channel_ws_tickets_tenant_id ON local_agent_channel_ws_tickets (tenant_id)")
    _enable_tenant_rls("local_agent_channel_ws_tickets")


def downgrade() -> None:
    op.drop_table("local_agent_channel_ws_tickets")
    op.drop_table("local_agent_channel_events")
    op.drop_table("local_agent_channel_messages")
    op.drop_table("local_agent_channel_sessions")
    op.drop_table("local_agent_channels")
