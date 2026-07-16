"""Add durable post-admission HumanInput dispatch authority.

Revision ID: session_v2_input_dispatch_0716
Revises: session_v2_admission_revision_0716
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "session_v2_input_dispatch_0716"
down_revision = "session_v2_admission_revision_0716"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_input_admissions",
        sa.Column("dispatch_state", sa.String(length=40), nullable=False, server_default="not_applicable"),
    )
    op.add_column(
        "session_input_admissions",
        sa.Column(
            "dispatch_receipt_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "session_input_admissions",
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("session_input_admissions", sa.Column("dispatch_last_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_session_input_admissions_dispatch_state",
        "session_input_admissions",
        "dispatch_state IN ('not_applicable','pending','dispatching','dispatched','needs_reconciliation')",
    )
    op.execute(
        """
        UPDATE session_input_admissions
        SET dispatch_state='pending'
        WHERE state='admitted'
        """
    )


def downgrade() -> None:
    op.execute("LOCK TABLE session_input_admissions IN SHARE MODE")
    op.execute(
        """
        DO $session_v2_input_dispatch_downgrade_guard$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM session_input_admissions
            WHERE dispatch_state <> 'not_applicable'
               OR dispatch_attempts <> 0
               OR dispatch_receipt_json <> '{}'::jsonb
               OR dispatch_last_error IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'session_v2_input_dispatch_downgrade_blocked: durable dispatch evidence exists'
              USING ERRCODE='23514';
          END IF;
        END;
        $session_v2_input_dispatch_downgrade_guard$
        """
    )
    op.drop_constraint(
        "ck_session_input_admissions_dispatch_state",
        "session_input_admissions",
        type_="check",
    )
    op.drop_column("session_input_admissions", "dispatch_last_error")
    op.drop_column("session_input_admissions", "dispatch_attempts")
    op.drop_column("session_input_admissions", "dispatch_receipt_json")
    op.drop_column("session_input_admissions", "dispatch_state")
