"""Add RuntimeTask claim and lease columns.

Revision ID: runtime_task_claim_lease_0702
Revises: web_chat_final_message_idempotency_0702
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "runtime_task_claim_lease_0702"
down_revision = "web_chat_final_message_idempotency_0702"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runtime_tasks", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "runtime_tasks",
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("runtime_tasks", sa.Column("claimed_by", sa.String(length=200), nullable=True))
    op.add_column("runtime_tasks", sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "runtime_tasks",
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index(op.f("ix_runtime_tasks_scheduled_at"), "runtime_tasks", ["scheduled_at"], unique=False)
    op.create_index(op.f("ix_runtime_tasks_priority"), "runtime_tasks", ["priority"], unique=False)
    op.create_index(op.f("ix_runtime_tasks_claimed_by"), "runtime_tasks", ["claimed_by"], unique=False)
    op.create_index(op.f("ix_runtime_tasks_claim_expires_at"), "runtime_tasks", ["claim_expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_runtime_tasks_claim_expires_at"), table_name="runtime_tasks")
    op.drop_index(op.f("ix_runtime_tasks_claimed_by"), table_name="runtime_tasks")
    op.drop_index(op.f("ix_runtime_tasks_priority"), table_name="runtime_tasks")
    op.drop_index(op.f("ix_runtime_tasks_scheduled_at"), table_name="runtime_tasks")
    op.drop_column("runtime_tasks", "attempt_count")
    op.drop_column("runtime_tasks", "claim_expires_at")
    op.drop_column("runtime_tasks", "claimed_by")
    op.drop_column("runtime_tasks", "priority")
    op.drop_column("runtime_tasks", "scheduled_at")
