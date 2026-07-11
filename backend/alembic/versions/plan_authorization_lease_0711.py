"""Bind confirmed plans to single-use action leases.

Revision ID: plan_authorization_lease_0711
Revises: runtime_notification_outbox_0710
Create Date: 2026-07-11

The lease rows reuse ``approval_requests`` and therefore require no duplicate
authorization table.  This migration adds only the durable artifact evidence
column needed by business tasks.  Historical confirmed plans predate exact
requester/session/action bindings; they are safely expired instead of being
silently upgraded into broad capabilities.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "plan_authorization_lease_0711"
down_revision = "runtime_notification_outbox_0710"
branch_labels = None
depends_on = None

LEGACY_INVALIDATION_MARKER = revision


def upgrade() -> None:
    op.add_column("tasks", sa.Column("plan_authorization", postgresql.JSONB(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE agent_plan_requests
            SET status = 'expired',
                expires_at = now(),
                metadata_json = COALESCE(metadata_json, '{}'::jsonb)
                    || jsonb_build_object(
                        'legacy_plan_authorization_invalidated_by',
                        :marker,
                        'legacy_plan_authorization_invalidated_at',
                        now(),
                        'legacy_plan_authorization_previous_expires_at',
                        expires_at
                    ),
                updated_at = now()
            WHERE status = 'confirmed'
              AND NOT EXISTS (
                    SELECT 1
                    FROM approval_requests AS lease
                    WHERE lease.action_type = 'plan_authorization'
                      AND lease.details->>'plan_id' = agent_plan_requests.id::text
                )
            """
        ).bindparams(marker=LEGACY_INVALIDATION_MARKER)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agent_plan_requests
            SET status = 'confirmed',
                expires_at = CASE
                    WHEN metadata_json->'legacy_plan_authorization_previous_expires_at' IS NULL
                      OR metadata_json->'legacy_plan_authorization_previous_expires_at' = 'null'::jsonb
                    THEN NULL
                    ELSE (metadata_json->>'legacy_plan_authorization_previous_expires_at')::timestamptz
                END,
                metadata_json = COALESCE(metadata_json, '{}'::jsonb)
                    - 'legacy_plan_authorization_invalidated_by'
                    - 'legacy_plan_authorization_invalidated_at'
                    - 'legacy_plan_authorization_previous_expires_at',
                updated_at = now()
            WHERE metadata_json->>'legacy_plan_authorization_invalidated_by' = :marker
            """
        ).bindparams(marker=LEGACY_INVALIDATION_MARKER)
    )
    op.drop_column("tasks", "plan_authorization")
