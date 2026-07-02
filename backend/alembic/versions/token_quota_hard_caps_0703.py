"""Add tenant and agent hard token quota fields.

Revision ID: token_quota_hard_caps_0703
Revises: invocation_span_execution_identity_0703
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op


revision = "token_quota_hard_caps_0703"
down_revision = "invocation_span_execution_identity_0703"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS quota_tokens_per_day INTEGER")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS quota_tokens_per_month INTEGER")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tokens_used_today INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tokens_used_month INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tokens_used_total INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tokens_reset_at TIMESTAMPTZ")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS quota_tokens_per_day INTEGER")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS quota_tokens_per_month INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS quota_tokens_per_month")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS quota_tokens_per_day")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS tokens_reset_at")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS tokens_used_total")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS tokens_used_month")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS tokens_used_today")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS quota_tokens_per_month")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS quota_tokens_per_day")
