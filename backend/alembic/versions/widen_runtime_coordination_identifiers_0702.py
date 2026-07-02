"""Widen runtime and coordination identifiers.

Revision ID: widen_runtime_coordination_identifiers_0702
Revises: runtime_task_claim_lease_0702
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "widen_runtime_coordination_identifiers_0702"
down_revision = "runtime_task_claim_lease_0702"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "runtime_tasks",
        "trace_id",
        existing_type=sa.String(length=255),
        type_=sa.String(length=512),
        existing_nullable=True,
    )
    op.alter_column(
        "runtime_tasks",
        "parent_session_id",
        existing_type=sa.String(length=100),
        type_=sa.String(length=512),
        existing_nullable=True,
    )
    op.alter_column(
        "runtime_tasks",
        "child_session_id",
        existing_type=sa.String(length=100),
        type_=sa.String(length=512),
        existing_nullable=True,
    )
    op.alter_column(
        "coordination_signals",
        "from_agent_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=512),
        existing_nullable=False,
    )
    op.alter_column(
        "coordination_signals",
        "to_agent_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=512),
        existing_nullable=False,
    )
    op.alter_column(
        "coordination_signals",
        "signal_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.alter_column(
        "coordination_signals",
        "thread_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=512),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "coordination_signals",
        "thread_id",
        existing_type=sa.String(length=512),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "coordination_signals",
        "signal_type",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "coordination_signals",
        "to_agent_id",
        existing_type=sa.String(length=512),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "coordination_signals",
        "from_agent_id",
        existing_type=sa.String(length=512),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "runtime_tasks",
        "child_session_id",
        existing_type=sa.String(length=512),
        type_=sa.String(length=100),
        existing_nullable=True,
    )
    op.alter_column(
        "runtime_tasks",
        "parent_session_id",
        existing_type=sa.String(length=512),
        type_=sa.String(length=100),
        existing_nullable=True,
    )
    op.alter_column(
        "runtime_tasks",
        "trace_id",
        existing_type=sa.String(length=512),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
