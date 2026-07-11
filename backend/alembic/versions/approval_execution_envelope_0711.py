"""Bind deferred approvals to the original tool execution envelope.

Revision ID: approval_execution_envelope_0711
Revises: plan_authorization_lease_0711
Create Date: 2026-07-11

Historical tool approvals cannot be upgraded safely because their session,
runtime-task, budget, permission, delegation, workspace and hook context was not
persisted. They are invalidated and require a fresh request. Downgrade restores
only rows changed by this migration using the recorded previous states.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "approval_execution_envelope_0711"
down_revision = "plan_authorization_lease_0711"
branch_labels = None
depends_on = None

LEGACY_INVALIDATION_MARKER = revision


def upgrade() -> None:
    op.add_column(
        "approval_requests",
        sa.Column("execution_envelope", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("execution_envelope_hash", sa.String(length=64), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE approval_requests
            SET details = COALESCE(details, '{}'::jsonb)
                    || jsonb_build_object(
                        'legacy_approval_invalidated_by', :marker,
                        'legacy_approval_invalidated_at', now(),
                        'legacy_approval_previous_status', status::text,
                        'legacy_approval_previous_execution_status', execution_status,
                        'legacy_approval_previous_resolved_at', resolved_at
                    ),
                status = 'rejected',
                execution_status = 'needs_reapproval',
                resolved_at = COALESCE(resolved_at, now())
            WHERE tool_name IS NOT NULL
              AND execution_envelope IS NULL
              AND status IN ('pending', 'approved')
            """
        ).bindparams(marker=LEGACY_INVALIDATION_MARKER)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE approval_requests
            SET status = (details->>'legacy_approval_previous_status')::approval_status_enum,
                execution_status = details->>'legacy_approval_previous_execution_status',
                resolved_at = CASE
                    WHEN details->'legacy_approval_previous_resolved_at' IS NULL
                      OR details->'legacy_approval_previous_resolved_at' = 'null'::jsonb
                    THEN NULL
                    ELSE (details->>'legacy_approval_previous_resolved_at')::timestamptz
                END,
                details = COALESCE(details, '{}'::jsonb)
                    - 'legacy_approval_invalidated_by'
                    - 'legacy_approval_invalidated_at'
                    - 'legacy_approval_previous_status'
                    - 'legacy_approval_previous_execution_status'
                    - 'legacy_approval_previous_resolved_at'
            WHERE details->>'legacy_approval_invalidated_by' = :marker
            """
        ).bindparams(marker=LEGACY_INVALIDATION_MARKER)
    )
    op.drop_column("approval_requests", "execution_envelope_hash")
    op.drop_column("approval_requests", "execution_envelope")
