"""Add Local Agent Bridge pairing and connection tables.

Revision ID: local_agent_bridge_0622
Revises: chat_artifact_delivery_0620
Create Date: 2026-06-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "local_agent_bridge_0622"
down_revision = "chat_artifact_delivery_0620"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table: str) -> bool:
    return _inspector().has_table(table)


def _column_exists(table: str, column: str) -> bool:
    return any(existing["name"] == column for existing in _inspector().get_columns(table))


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
    if not _column_exists("gateway_messages", "attachments_json"):
        op.add_column(
            "gateway_messages",
            sa.Column(
                "attachments_json",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
    if not _column_exists("gateway_messages", "client_message_id"):
        op.add_column("gateway_messages", sa.Column("client_message_id", sa.String(length=128), nullable=True))
    if not _column_exists("gateway_messages", "metadata_json"):
        op.add_column(
            "gateway_messages",
            sa.Column(
                "metadata_json",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_gateway_messages_client_message_id ON gateway_messages (client_message_id)")

    if not _table_exists("local_agent_bridge_connections"):
        op.create_table(
            "local_agent_bridge_connections",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("device_name", sa.String(length=255), nullable=False),
            sa.Column("client_kind", sa.String(length=64), nullable=False),
            sa.Column("device_fingerprint", sa.String(length=255), nullable=False),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_ip", sa.String(length=64), nullable=True),
            sa.Column("last_seen_user_agent", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_local_bridge_connection_token_hash"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_bridge_connections_agent_status ON local_agent_bridge_connections (agent_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_bridge_connections_tenant_id ON local_agent_bridge_connections (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_bridge_connections_user_id ON local_agent_bridge_connections (user_id)")
    _enable_tenant_rls("local_agent_bridge_connections")

    if not _table_exists("local_agent_bridge_pairing_sessions"):
        op.create_table(
            "local_agent_bridge_pairing_sessions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("pairing_code_hash", sa.String(length=128), nullable=False),
            sa.Column("device_code_hash", sa.String(length=128), nullable=False),
            sa.Column("device_name", sa.String(length=255), nullable=False),
            sa.Column("client_kind", sa.String(length=64), nullable=False),
            sa.Column("device_fingerprint", sa.String(length=255), nullable=False),
            sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["connection_id"], ["local_agent_bridge_connections.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("pairing_code_hash", name="uq_local_bridge_pairing_code_hash"),
            sa.UniqueConstraint("device_code_hash", name="uq_local_bridge_device_code_hash"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_bridge_pairing_status ON local_agent_bridge_pairing_sessions (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_bridge_pairing_agent_id ON local_agent_bridge_pairing_sessions (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_local_bridge_pairing_tenant_id ON local_agent_bridge_pairing_sessions (tenant_id)")
    _enable_tenant_rls("local_agent_bridge_pairing_sessions")


def downgrade() -> None:
    op.drop_table("local_agent_bridge_pairing_sessions")
    op.drop_table("local_agent_bridge_connections")
    if _column_exists("gateway_messages", "metadata_json"):
        op.drop_column("gateway_messages", "metadata_json")
    if _column_exists("gateway_messages", "client_message_id"):
        op.drop_column("gateway_messages", "client_message_id")
    if _column_exists("gateway_messages", "attachments_json"):
        op.drop_column("gateway_messages", "attachments_json")
