"""Add durable runtime-budget transition delivery.

Revision ID: budget_transition_outbox_0711
Revises: workflow_promotion_proposals_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import create_index_if_missing, create_table_if_missing


revision = "budget_transition_outbox_0711"
down_revision = "workflow_promotion_proposals_0711"
branch_labels = None
depends_on = None

_BUDGET_TRANSITION_TABLES = ("budget_transition_outbox",)


def upgrade() -> None:
    create_table_if_missing(
        op,
        "budget_transition_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "budget_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_budget_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "budget_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_budget_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("transition", sa.String(length=40), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
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
        sa.Column(
            "runtime_task_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("channel_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.String(length=40), server_default="web", nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "delivery_target_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "delivery_receipts_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            name="ck_budget_transition_outbox_status",
        ),
        sa.UniqueConstraint("budget_event_id", name="uq_budget_transition_outbox_event"),
    )
    for column in (
        "tenant_id",
        "budget_run_id",
        "budget_event_id",
        "transition",
        "session_id",
        "agent_id",
        "user_id",
        "external_principal_id",
        "runtime_task_id",
        "channel_config_id",
        "status",
        "available_at",
        "locked_by",
    ):
        create_index_if_missing(op, f"ix_budget_transition_outbox_{column}", "budget_transition_outbox", [column])
    create_index_if_missing(
        op,
        "ix_budget_transition_outbox_claim",
        "budget_transition_outbox",
        ["status", "available_at", "locked_at"],
    )
    create_index_if_missing(
        op,
        "uq_chat_transcript_budget_transition_causation",
        "chat_transcript_events",
        ["session_id", "causation_id", "event_type"],
        unique=True,
        postgresql_where=sa.text(
            "causation_id IS NOT NULL AND event_type = 'runtime_budget_transition' "
            "AND metadata_json ? 'budget_transition_outbox_id'"
        ),
    )
    op.execute("ALTER TABLE budget_transition_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE budget_transition_outbox FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_budget_transition_outbox
        ON budget_transition_outbox
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            OR current_setting('app.rls_bypass', true) = 'on'
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            OR current_setting('app.rls_bypass', true) = 'on'
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION budget_transition_outbox_snapshot_immutable()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
               OR OLD.budget_run_id IS DISTINCT FROM NEW.budget_run_id
               OR OLD.budget_event_id IS DISTINCT FROM NEW.budget_event_id
               OR OLD.transition IS DISTINCT FROM NEW.transition
               OR OLD.session_id IS DISTINCT FROM NEW.session_id
               OR OLD.agent_id IS DISTINCT FROM NEW.agent_id
               OR OLD.user_id IS DISTINCT FROM NEW.user_id
               OR OLD.external_principal_id IS DISTINCT FROM NEW.external_principal_id
               OR OLD.runtime_task_id IS DISTINCT FROM NEW.runtime_task_id
               OR OLD.channel_config_id IS DISTINCT FROM NEW.channel_config_id
               OR OLD.channel IS DISTINCT FROM NEW.channel
               OR OLD.content IS DISTINCT FROM NEW.content
               OR OLD.delivery_target_json IS DISTINCT FROM NEW.delivery_target_json
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'budget transition delivery snapshot is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_budget_transition_outbox_snapshot_immutable
        BEFORE UPDATE ON budget_transition_outbox
        FOR EACH ROW EXECUTE FUNCTION budget_transition_outbox_snapshot_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_budget_transition_outbox_snapshot_immutable ON budget_transition_outbox")
    op.execute("DROP FUNCTION IF EXISTS budget_transition_outbox_snapshot_immutable()")
    op.drop_index(
        "uq_chat_transcript_budget_transition_causation",
        table_name="chat_transcript_events",
    )
    op.drop_table("budget_transition_outbox")
