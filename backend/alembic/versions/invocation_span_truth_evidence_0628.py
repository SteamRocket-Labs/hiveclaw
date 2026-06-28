"""Add truth evidence fields to invocation spans.

Revision ID: invocation_span_truth_evidence_0628
Revises: retire_openclaw_gateway_0627
Create Date: 2026-06-28 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "invocation_span_truth_evidence_0628"
down_revision = "retire_openclaw_gateway_0627"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invocation_spans",
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "invocation_spans",
        sa.Column(
            "truth_evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("invocation_spans", "truth_evidence_json")
    op.drop_column("invocation_spans", "evidence_refs")
