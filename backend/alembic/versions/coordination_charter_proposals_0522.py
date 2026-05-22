"""Add coordination_{leases,signals,checkpoints} + charter_proposals tables.

Revision ID: coordination_charter_0522
Revises: add_llm_error_activity_enum_0513
Create Date: 2026-05-22

Phase 14 + 15 redo: durable coordination primitives and charter
calibration proposals move from per-process sqlite files into Hive's
PostgreSQL store so tenant isolation, backup, and cross-worker
visibility apply.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "coordination_charter_0522"
down_revision: Union[str, None] = "add_llm_error_activity_enum_0513"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _table_exists(conn, name: str) -> bool:
    result = conn.execute(
        sa.text("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = :name)"),
        {"name": name},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, "coordination_leases"):
        op.create_table(
            "coordination_leases",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("task_key", sa.String(length=255), nullable=False),
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("tenant_id", "task_key", name="uq_coordination_lease_tenant_task"),
        )
        op.create_index("ix_coordination_leases_tenant_id", "coordination_leases", ["tenant_id"])

    if not _table_exists(conn, "coordination_signals"):
        op.create_table(
            "coordination_signals",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("from_agent_id", sa.String(length=64), nullable=False),
            sa.Column("to_agent_id", sa.String(length=64), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("signal_type", sa.String(length=64), nullable=False),
            sa.Column("thread_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_coordination_signals_tenant_id", "coordination_signals", ["tenant_id"])
        op.create_index("ix_coordination_signals_to_agent_id", "coordination_signals", ["to_agent_id"])
        op.create_index("ix_coordination_signals_thread_id", "coordination_signals", ["thread_id"])

    if not _table_exists(conn, "coordination_checkpoints"):
        op.create_table(
            "coordination_checkpoints",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("action", sa.String(length=255), nullable=False),
            sa.Column("approver_id", sa.String(length=64), nullable=False),
            sa.Column(
                "escalation_chain",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("current_approver_id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
            sa.Column(
                "metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_coordination_checkpoints_tenant_id", "coordination_checkpoints", ["tenant_id"])
        op.create_index("ix_coordination_checkpoints_status", "coordination_checkpoints", ["status"])

    if not _table_exists(conn, "charter_proposals"):
        op.create_table(
            "charter_proposals",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("decision_id", sa.String(length=128), nullable=False),
            sa.Column("action", sa.String(length=255), nullable=False),
            sa.Column("proposal_kind", sa.String(length=64), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_by", sa.String(length=64), nullable=True),
            sa.Column("decision_reason", sa.Text(), nullable=True),
        )
        op.create_index("ix_charter_proposals_tenant_id", "charter_proposals", ["tenant_id"])
        op.create_index("ix_charter_proposals_agent_id", "charter_proposals", ["agent_id"])
        op.create_index("ix_charter_proposals_status", "charter_proposals", ["status"])


def downgrade() -> None:
    op.drop_table("charter_proposals")
    op.drop_table("coordination_checkpoints")
    op.drop_table("coordination_signals")
    op.drop_table("coordination_leases")
