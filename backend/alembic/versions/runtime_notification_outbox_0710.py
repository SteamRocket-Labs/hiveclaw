"""Add durable runtime completion notification outbox.

Revision ID: runtime_notification_outbox_0710
Revises: workflow_confirmation_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "runtime_notification_outbox_0710"
down_revision = "workflow_confirmation_0710"
branch_labels = None
depends_on = None

_RUNTIME_NOTIFICATION_OUTBOX_TABLES = ("runtime_notification_outbox",)


def upgrade() -> None:
    op.create_table(
        "runtime_notification_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_run_id", sa.String(length=200), nullable=False),
        sa.Column(
            "parent_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "child_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("child_agent_name", sa.String(length=200), nullable=True),
        sa.Column("terminal_status", sa.String(length=40), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("delivery_mode", sa.String(length=32), server_default="parent_continuation", nullable=False),
        sa.Column("artifacts_json", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("payload_rank", sa.Integer(), server_default="100", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_by", sa.String(length=200), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivery_receipt_json", postgresql.JSONB(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('subagent','agent_team','workflow','trigger','delegation','a2a_delegation','runtime_budget')",
            name="ck_runtime_notification_outbox_source_kind",
        ),
        sa.CheckConstraint(
            "delivery_mode IN ('parent_continuation','session_projection')",
            name="ck_runtime_notification_outbox_delivery_mode",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','delivered','dead_letter')",
            name="ck_runtime_notification_outbox_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source_kind",
            "source_run_id",
            "parent_session_id",
            "terminal_status",
            name="uq_runtime_notification_outbox_delivery",
        ),
    )
    for name, columns in (
        ("ix_runtime_notification_outbox_tenant_id", ["tenant_id"]),
        ("ix_runtime_notification_outbox_parent_session_id", ["parent_session_id"]),
        ("ix_runtime_notification_outbox_parent_agent_id", ["parent_agent_id"]),
        ("ix_runtime_notification_outbox_parent_user_id", ["parent_user_id"]),
        ("ix_runtime_notification_outbox_status", ["status"]),
        ("ix_runtime_notification_outbox_available_at", ["available_at"]),
        ("ix_runtime_notification_outbox_locked_by", ["locked_by"]),
        ("ix_runtime_notification_outbox_claim", ["status", "available_at", "locked_at"]),
    ):
        op.create_index(name, "runtime_notification_outbox", columns)

    op.execute("ALTER TABLE runtime_notification_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_notification_outbox FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_runtime_notification_outbox ON runtime_notification_outbox")
    op.execute(
        """
        CREATE POLICY tenant_isolation_runtime_notification_outbox ON runtime_notification_outbox
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

    op.create_index(
        "uq_chat_transcript_completion_causation",
        "chat_transcript_events",
        ["session_id", "causation_id", "event_type"],
        unique=True,
        postgresql_where=sa.text(
            "causation_id IS NOT NULL AND event_type = 'agent_task_notification' "
            "AND metadata_json ? 'completion_outbox_id'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_chat_transcript_completion_causation", table_name="chat_transcript_events")
    op.drop_table("runtime_notification_outbox")
