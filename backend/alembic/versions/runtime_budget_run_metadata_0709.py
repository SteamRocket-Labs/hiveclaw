"""Add metadata_json to runtime_budget_runs for the §2 finalization-lane state.

Revision ID: runtime_budget_run_metadata_0709
Revises: external_capability_rls_0709
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "runtime_budget_run_metadata_0709"
down_revision = "external_capability_rls_0709"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_budget_runs",
        sa.Column("metadata_json", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runtime_budget_runs", "metadata_json")
