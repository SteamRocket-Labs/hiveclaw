"""Add the tenant-governed durable external channel ingress inbox.

Revision ID: channel_ingress_inbox_0711
Revises: business_task_atomic_state_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import add_column_if_missing, create_index_if_missing, create_table_if_missing


revision = "channel_ingress_inbox_0711"
down_revision = "business_task_atomic_state_0711"
branch_labels = None
depends_on = None

_CHANNEL_INGRESS_TABLES = ("channel_ingress_events",)


def upgrade() -> None:
    create_table_if_missing(
        op,
        "channel_ingress_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("installation_ref", sa.String(length=200), nullable=False),
        sa.Column("provider_event_id", sa.String(length=512), nullable=False),
        sa.Column("handler_key", sa.String(length=100), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="received", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_by", sa.String(length=200), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "result_runtime_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "result_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("processing_receipt_json", postgresql.JSONB(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('received','processing','failed','processed','dead_letter')",
            name="ck_channel_ingress_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "installation_ref",
            "provider_event_id",
            name="uq_channel_ingress_provider_event",
        ),
    )
    for name, columns in (
        ("ix_channel_ingress_events_tenant_id", ["tenant_id"]),
        ("ix_channel_ingress_events_agent_id", ["agent_id"]),
        ("ix_channel_ingress_events_status", ["status"]),
        ("ix_channel_ingress_events_available_at", ["available_at"]),
        ("ix_channel_ingress_events_locked_by", ["locked_by"]),
        ("ix_channel_ingress_events_result_runtime_task_id", ["result_runtime_task_id"]),
        ("ix_channel_ingress_events_result_session_id", ["result_session_id"]),
        ("ix_channel_ingress_claim", ["status", "available_at", "locked_at"]),
    ):
        create_index_if_missing(op, name, "channel_ingress_events", columns)

    op.execute("ALTER TABLE channel_ingress_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE channel_ingress_events FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_channel_ingress_events ON channel_ingress_events")
    op.execute(
        """
        CREATE POLICY tenant_isolation_channel_ingress_events ON channel_ingress_events
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )

    add_column_if_missing(
        op,
        "chat_messages",
        sa.Column(
            "source_ingress_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_ingress_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    create_index_if_missing(
        op,
        "ix_chat_messages_source_ingress_event_id",
        "chat_messages",
        ["source_ingress_event_id"],
    )
    create_index_if_missing(
        op,
        "uq_chat_messages_ingress_user",
        "chat_messages",
        ["source_ingress_event_id"],
        unique=True,
        postgresql_where=sa.text("source_ingress_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_chat_messages_ingress_user", table_name="chat_messages")
    op.drop_index("ix_chat_messages_source_ingress_event_id", table_name="chat_messages")
    op.drop_column("chat_messages", "source_ingress_event_id")
    op.drop_table("channel_ingress_events")
