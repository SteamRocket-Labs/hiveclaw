"""Add durable terminal channel delivery outbox.

Revision ID: channel_delivery_outbox_0711
Revises: external_principals_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "channel_delivery_outbox_0711"
down_revision = "external_principals_0711"
branch_labels = None
depends_on = None

_CHANNEL_DELIVERY_OUTBOX_TABLES = ("channel_delivery_outbox",)


def upgrade() -> None:
    op.create_table(
        "channel_delivery_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runtime_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "external_principal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_principals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=False),
        sa.Column("delivery_kind", sa.String(length=32), server_default="terminal_result", nullable=False),
        sa.Column("terminal_status", sa.String(length=40), nullable=False),
        sa.Column("delivery_target_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("artifact_ids_json", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("delivery_receipts_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_by", sa.String(length=200), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','processing','delivered','dead_letter','needs_reconciliation')",
            name="ck_channel_delivery_outbox_status",
        ),
        sa.CheckConstraint(
            "delivery_kind IN ('terminal_result','interactive_prompt')",
            name="ck_channel_delivery_outbox_kind",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "runtime_task_id",
            "delivery_kind",
            "target_hash",
            name="uq_channel_delivery_outbox_runtime_target",
        ),
    )
    for name, columns in (
        ("ix_channel_delivery_outbox_tenant_id", ["tenant_id"]),
        ("ix_channel_delivery_outbox_runtime_task_id", ["runtime_task_id"]),
        ("ix_channel_delivery_outbox_agent_id", ["agent_id"]),
        ("ix_channel_delivery_outbox_session_id", ["session_id"]),
        ("ix_channel_delivery_outbox_user_id", ["user_id"]),
        ("ix_channel_delivery_outbox_external_principal_id", ["external_principal_id"]),
        ("ix_channel_delivery_outbox_channel_config_id", ["channel_config_id"]),
        ("ix_channel_delivery_outbox_channel", ["channel"]),
        ("ix_channel_delivery_outbox_status", ["status"]),
        ("ix_channel_delivery_outbox_available_at", ["available_at"]),
        ("ix_channel_delivery_outbox_locked_by", ["locked_by"]),
        ("ix_channel_delivery_outbox_claim", ["status", "available_at", "locked_at"]),
    ):
        op.create_index(name, "channel_delivery_outbox", columns)

    op.execute("ALTER TABLE channel_delivery_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE channel_delivery_outbox FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_channel_delivery_outbox ON channel_delivery_outbox")
    op.execute(
        """
        CREATE POLICY tenant_isolation_channel_delivery_outbox ON channel_delivery_outbox
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


def downgrade() -> None:
    op.drop_table("channel_delivery_outbox")
