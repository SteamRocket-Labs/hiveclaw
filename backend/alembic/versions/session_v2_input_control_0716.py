"""Make replacement admission precede its child cancellation control.

Revision ID: session_v2_input_control_0716
Revises: session_v2_0716
"""

from __future__ import annotations

from alembic import op

from migration_snapshots.session_v2_contract_0716 import (
    build_session_event_contract_function_sql as build_parent_event_contract_function_sql,
    build_session_tenant_binding_function_sql as build_parent_tenant_binding_function_sql,
)
from migration_snapshots.session_v2_input_control_contract_0716 import (
    build_session_event_contract_function_sql,
    build_session_tenant_binding_function_sql,
)


revision = "session_v2_input_control_0716"
down_revision = "session_v2_0716"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # §11.5 is input-first: an admitted replacement creates a durable requested
    # saga before any cancel ControlInput exists.  The child identifiers become
    # mandatory through the state-aware authority trigger once cancellation
    # begins.
    op.alter_column("session_turn_replacements", "cancel_control_id", nullable=True)
    op.alter_column("session_turn_replacements", "cancel_command_id", nullable=True)
    op.execute(build_session_event_contract_function_sql())
    op.execute(build_session_tenant_binding_function_sql())


def _assert_schema_preserving_downgrade() -> None:
    op.execute("LOCK TABLE session_turn_replacements IN SHARE MODE")
    op.execute(
        """
        DO $session_v2_input_control_downgrade_guard$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM session_turn_replacements
            WHERE cancel_control_id IS NULL OR cancel_command_id IS NULL
          ) THEN
            RAISE EXCEPTION
              'session_v2_input_control_downgrade_blocked: input-first replacement evidence exists'
              USING ERRCODE='23514';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM chat_transcript_events AS event
            JOIN session_commands AS command ON command.id=event.command_id
            JOIN session_turn_inputs AS input ON input.id=event.input_id
            WHERE command.namespace='turn_replacement'
              AND command.causation_command_id=input.command_id
          ) THEN
            RAISE EXCEPTION
              'session_v2_input_control_downgrade_blocked: replacement event lineage exists'
              USING ERRCODE='23514';
          END IF;
        END;
        $session_v2_input_control_downgrade_guard$
        """
    )


def downgrade() -> None:
    # Schema-preserving: never delete, synthesize, or rewrite replacement
    # evidence to make the older shape fit.  Refuse the downgrade if an
    # input-first requested saga exists.
    _assert_schema_preserving_downgrade()
    op.execute(build_parent_event_contract_function_sql())
    op.execute(build_parent_tenant_binding_function_sql())
    op.alter_column("session_turn_replacements", "cancel_command_id", nullable=False)
    op.alter_column("session_turn_replacements", "cancel_control_id", nullable=False)
