"""Add runtime budget §10 circuit-breaker dimensions and counters.

Adds the failure / reconciliation / child-failure-ratio / parent-invocation
breaker thresholds to policies and runs, the ground-truth breaker counters on
runs, and the §7.4 runtime_tasks budget lineage columns
(root_runtime_task_id / budget_snapshot_json).

Revision ID: runtime_budget_breaker_dims_0709
Revises: capability_factor_intake_0709
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "runtime_budget_breaker_dims_0709"
down_revision = "capability_factor_intake_0709"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # §10 breaker thresholds on the policy template.
    op.add_column("runtime_budget_policies", sa.Column("max_failures", sa.Integer(), nullable=True))
    op.add_column("runtime_budget_policies", sa.Column("max_needs_reconciliation", sa.Integer(), nullable=True))
    op.add_column("runtime_budget_policies", sa.Column("max_child_failure_ratio", sa.Float(), nullable=True))
    op.add_column("runtime_budget_policies", sa.Column("max_parent_invocations", sa.Integer(), nullable=True))

    # §10 breaker thresholds snapshotted onto each run.
    op.add_column("runtime_budget_runs", sa.Column("max_failures", sa.Integer(), nullable=True))
    op.add_column("runtime_budget_runs", sa.Column("max_needs_reconciliation", sa.Integer(), nullable=True))
    op.add_column("runtime_budget_runs", sa.Column("max_child_failure_ratio", sa.Float(), nullable=True))
    op.add_column("runtime_budget_runs", sa.Column("max_parent_invocations", sa.Integer(), nullable=True))

    # §10 breaker counters with real write points.
    op.add_column(
        "runtime_budget_runs",
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "runtime_budget_runs",
        sa.Column("needs_reconciliation_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "runtime_budget_runs",
        sa.Column("parent_invocations", sa.Integer(), server_default="0", nullable=False),
    )

    # §7.4 runtime_tasks budget lineage columns.
    op.add_column(
        "runtime_tasks",
        sa.Column("root_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "runtime_tasks",
        sa.Column("budget_snapshot_json", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_runtime_tasks_root_runtime_task_id",
        "runtime_tasks",
        ["root_runtime_task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_tasks_root_runtime_task_id", table_name="runtime_tasks")
    op.drop_column("runtime_tasks", "budget_snapshot_json")
    op.drop_column("runtime_tasks", "root_runtime_task_id")

    op.drop_column("runtime_budget_runs", "parent_invocations")
    op.drop_column("runtime_budget_runs", "needs_reconciliation_count")
    op.drop_column("runtime_budget_runs", "failures")
    op.drop_column("runtime_budget_runs", "max_parent_invocations")
    op.drop_column("runtime_budget_runs", "max_child_failure_ratio")
    op.drop_column("runtime_budget_runs", "max_needs_reconciliation")
    op.drop_column("runtime_budget_runs", "max_failures")

    op.drop_column("runtime_budget_policies", "max_parent_invocations")
    op.drop_column("runtime_budget_policies", "max_child_failure_ratio")
    op.drop_column("runtime_budget_policies", "max_needs_reconciliation")
    op.drop_column("runtime_budget_policies", "max_failures")
