"""Allow long agent role descriptions.

Revision ID: agent_role_description_text_0620
Revises: decision_trace_pg_store_0615
Create Date: 2026-06-20
"""

from __future__ import annotations

from alembic import op


revision = "agent_role_description_text_0620"
down_revision = "decision_trace_pg_store_0615"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents ALTER COLUMN role_description TYPE TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE agents ALTER COLUMN role_description TYPE VARCHAR(500)")
