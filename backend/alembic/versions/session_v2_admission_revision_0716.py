"""Preserve one immutable input-admission attempt per input revision.

Revision ID: session_v2_admission_revision_0716
Revises: session_v2_input_control_0716
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_snapshots.session_v2_admission_revision_contract_0716 import (
    build_session_event_contract_function_sql,
    build_session_tenant_binding_function_sql,
)
from migration_snapshots.session_v2_input_control_contract_0716 import (
    build_session_event_contract_function_sql as build_parent_event_contract_function_sql,
    build_session_tenant_binding_function_sql as build_parent_tenant_binding_function_sql,
)


revision = "session_v2_admission_revision_0716"
down_revision = "session_v2_input_control_0716"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_input_admissions",
        sa.Column("input_revision", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE session_input_admissions
        SET input_revision=1
        """
    )
    # Before this revision there could only be one admission row per input and
    # it was created with the initial accepted bytes.  Preserve that immutable
    # attempt as revision 1.  A later parent revision has no proof of a matching
    # Hook evaluation, so quarantine the current input/command rather than
    # silently relabeling old Hook truth as current.
    op.execute(
        """
        UPDATE session_turn_inputs AS input
        SET status='needs_reconciliation',
            recovery_owner='session_v2_admission_revision_backfill',
            version=input.version+1
        WHERE input.revision > 1
          AND EXISTS (
            SELECT 1 FROM session_input_admissions AS admission
            WHERE admission.input_id=input.id
          )
        """
    )
    op.execute(
        """
        UPDATE session_commands AS command
        SET status='needs_reconciliation',
            rejection_json=jsonb_build_object(
              'reason_code','legacy_input_revision_admission_ambiguous',
              'recovery_owner','session_v2_admission_revision_backfill'
            )
        WHERE EXISTS (
          SELECT 1 FROM session_turn_inputs AS input
          WHERE input.command_id=command.id AND input.revision > 1
        )
        """
    )
    op.alter_column("session_input_admissions", "input_revision", nullable=False)
    op.drop_constraint(
        "uq_session_input_admissions_input",
        "session_input_admissions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_session_input_admissions_input_revision",
        "session_input_admissions",
        ["input_id", "input_revision"],
    )
    op.execute(build_session_event_contract_function_sql())
    op.execute(build_session_tenant_binding_function_sql())


def downgrade() -> None:
    op.execute("LOCK TABLE session_input_admissions IN SHARE MODE")
    op.execute(
        """
        DO $session_v2_admission_revision_downgrade_guard$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM session_input_admissions
            WHERE input_revision > 1
          ) OR EXISTS (
            SELECT input_id FROM session_input_admissions
            GROUP BY input_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'session_v2_admission_revision_downgrade_blocked: revisioned admission evidence exists'
              USING ERRCODE='23514';
          END IF;
        END;
        $session_v2_admission_revision_downgrade_guard$
        """
    )
    op.execute(build_parent_event_contract_function_sql())
    op.execute(build_parent_tenant_binding_function_sql())
    op.drop_constraint(
        "uq_session_input_admissions_input_revision",
        "session_input_admissions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_session_input_admissions_input",
        "session_input_admissions",
        ["input_id"],
    )
    op.drop_column("session_input_admissions", "input_revision")
