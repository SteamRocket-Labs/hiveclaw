"""Add execution identity fields to invocation spans.

Revision ID: invocation_span_execution_identity_0703
Revises: add_agent_list_performance_indexes_0702
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op


revision = "invocation_span_execution_identity_0703"
down_revision = "add_agent_list_performance_indexes_0702"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE invocation_spans "
        "ADD COLUMN IF NOT EXISTS execution_identity_type VARCHAR(20)"
    )
    op.execute(
        "ALTER TABLE invocation_spans "
        "ADD COLUMN IF NOT EXISTS execution_identity_id UUID"
    )
    op.execute(
        "ALTER TABLE invocation_spans "
        "ADD COLUMN IF NOT EXISTS execution_identity_label VARCHAR(200)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invocation_spans_execution_identity "
        "ON invocation_spans (tenant_id, execution_identity_type, execution_identity_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_invocation_spans_execution_identity")
    op.execute("ALTER TABLE invocation_spans DROP COLUMN IF EXISTS execution_identity_label")
    op.execute("ALTER TABLE invocation_spans DROP COLUMN IF EXISTS execution_identity_id")
    op.execute("ALTER TABLE invocation_spans DROP COLUMN IF EXISTS execution_identity_type")
