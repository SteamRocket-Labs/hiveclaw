"""Backfill bounded confirmation TTL for canonical HR drafts.

Revision ID: hr_draft_recovery_0712
Revises: tenant_null_semantics_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op


revision = "hr_draft_recovery_0712"
down_revision = "tenant_null_semantics_0712"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hr_creation_drafts DISABLE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE hr_creation_drafts
        SET expires_at = created_at + INTERVAL '7 days'
        WHERE status = 'awaiting_confirmation'
          AND expires_at IS NULL
        """
    )
    op.execute("ALTER TABLE hr_creation_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hr_creation_drafts FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Secure downgrade: an established expiry is durable user-facing evidence.
    # Clearing it would resurrect stale confirmations, so rollback preserves it.
    pass
