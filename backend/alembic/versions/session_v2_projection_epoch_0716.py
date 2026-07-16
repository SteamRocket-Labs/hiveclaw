"""Allow byte-faithful T0 projection after a legacy writer generation drains.

Revision ID: session_v2_projection_epoch_0716
Revises: session_v2_permission_tool_0716
"""

from __future__ import annotations

from alembic import op

from migration_snapshots.session_v2_projection_epoch_contract_0716 import (
    build_session_event_contract_function_sql,
)


revision = "session_v2_projection_epoch_0716"
down_revision = "session_v2_permission_tool_0716"
branch_labels = None
depends_on = None


def _install_projection_safe_event_contract() -> None:
    op.execute(build_session_event_contract_function_sql())
    op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_session_event_v2_contract() FROM PUBLIC")


def upgrade() -> None:
    _install_projection_safe_event_contract()


def downgrade() -> None:
    # Compatibility rollback must not strand already-committed transcript
    # evidence or reopen semantic mutation while older application code drains.
    _install_projection_safe_event_contract()
