"""Add durable Workflow completion-signal outbox.

Revision ID: workflow_completion_outbox_0713
Revises: workflow_quota_reservations_0713
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision = "workflow_completion_outbox_0713"
down_revision = "workflow_quota_reservations_0713"
branch_labels = None
depends_on = None

_WORKFLOW_COMPLETION_OUTBOX_TABLES = ("workflow_completion_outbox",)


def upgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.create_table(
        "workflow_completion_outbox",
        sa.Column("id", UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runtime_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("terminal_status", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("locked_by", sa.String(length=200), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending','processing','delivered','dead_letter')",
            name="ck_workflow_completion_outbox_status",
        ),
        sa.CheckConstraint(
            "terminal_status = 'completed'",
            name="ck_workflow_completion_outbox_terminal_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "terminal_status",
            name="uq_workflow_completion_outbox_run_status",
        ),
    )
    for column in ("tenant_id", "run_id", "agent_id"):
        op.create_index(f"ix_workflow_completion_outbox_{column}", "workflow_completion_outbox", [column])
    op.create_index(
        "ix_workflow_completion_outbox_claim",
        "workflow_completion_outbox",
        ["status", "available_at", "locked_at"],
    )
    op.execute("ALTER TABLE workflow_completion_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_completion_outbox FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_workflow_completion_outbox
        ON workflow_completion_outbox
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.drop_table("workflow_completion_outbox")
