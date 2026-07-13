"""Add idempotent Workflow leaf quota reservation receipts.

Revision ID: workflow_quota_reservations_0713
Revises: system_plan_runtime_task_0713
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision = "workflow_quota_reservations_0713"
down_revision = "system_plan_runtime_task_0713"
branch_labels = None
depends_on = None

_WORKFLOW_QUOTA_RESERVATION_TABLES = ("workflow_quota_reservations",)


def upgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.create_table(
        "workflow_quota_reservations",
        sa.Column("id", UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runtime_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("logical_key", sa.String(length=500), nullable=False),
        sa.Column("reservation_key", sa.String(length=540), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("estimated_tokens", sa.BigInteger(), nullable=False),
        # This table is introduced by this revision, so there are no legacy
        # rows to rewrite. The server default is nevertheless required for
        # archive/import writers that omit the new lifecycle field.
        sa.Column("state", sa.String(length=32), nullable=False, server_default="reserved"),
        sa.Column("step_id", sa.String(length=100), nullable=True),
        sa.Column("leaf_id", sa.String(length=200), nullable=True),
        sa.Column("input_hash", sa.String(length=80), nullable=True),
        sa.Column("actual_tokens", sa.BigInteger(), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_required_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_reason", sa.Text(), nullable=True),
        sa.Column("reconciliation_operation_id", sa.String(length=64), nullable=True),
        sa.Column("settlement_reason", sa.Text(), nullable=True),
        sa.Column("repair_deferred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "run_id",
            "logical_key",
            "attempt",
            name="uq_workflow_quota_reservation_attempt",
        ),
        sa.UniqueConstraint(
            "run_id",
            "reservation_key",
            name="uq_workflow_quota_reservation_key",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'executing', 'needs_reconciliation', 'settled')",
            name="ck_workflow_quota_reservation_state",
        ),
    )
    op.create_index(
        "ix_workflow_quota_reservations_tenant_id",
        "workflow_quota_reservations",
        ["tenant_id"],
    )
    op.create_index(
        "ix_workflow_quota_reservations_run_id",
        "workflow_quota_reservations",
        ["run_id"],
    )
    op.create_index(
        "ix_workflow_quota_reservations_run_state",
        "workflow_quota_reservations",
        ["run_id", "state"],
    )
    op.create_index(
        "ix_workflow_quota_reservations_state_created",
        "workflow_quota_reservations",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_workflow_quota_reservations_repair_scan",
        "workflow_quota_reservations",
        ["state", "repair_deferred_at", "created_at"],
    )
    op.execute("ALTER TABLE workflow_quota_reservations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_quota_reservations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_workflow_quota_reservations
        ON workflow_quota_reservations
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.drop_table("workflow_quota_reservations")
