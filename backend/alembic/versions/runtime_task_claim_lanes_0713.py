"""Add sargable RuntimeTask claim-lane indexes.

Revision ID: runtime_task_claim_lanes_0713
Revises: system_plan_outbox_0713
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "runtime_task_claim_lanes_0713"
down_revision = "system_plan_outbox_0713"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_runtime_tasks_claim_normal_lane",
        "runtime_tasks",
        ["task_type", sa.text("priority DESC"), "created_at", "id"],
        postgresql_where=sa.text("status IN ('pending', 'resumable')"),
    )
    op.create_index(
        "ix_runtime_tasks_claim_aged_lane",
        "runtime_tasks",
        ["task_type", "created_at", "id"],
        postgresql_where=sa.text("status IN ('pending', 'resumable', 'running')"),
    )
    op.create_index(
        "ix_runtime_tasks_claim_expired_lane",
        "runtime_tasks",
        ["task_type", "claim_expires_at", sa.text("priority DESC"), "created_at", "id"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_tasks_claim_expired_lane", table_name="runtime_tasks")
    op.drop_index("ix_runtime_tasks_claim_aged_lane", table_name="runtime_tasks")
    op.drop_index("ix_runtime_tasks_claim_normal_lane", table_name="runtime_tasks")
