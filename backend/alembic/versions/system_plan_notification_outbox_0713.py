"""Allow durable System Plan runtime notifications.

Revision ID: system_plan_outbox_0713
Revises: workflow_completion_outbox_0713
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "system_plan_outbox_0713"
down_revision = "workflow_completion_outbox_0713"
branch_labels = None
depends_on = None

_SOURCE_KINDS_LEGACY = (
    "'subagent','agent_team','workflow','trigger','delegation','a2a_delegation','runtime_budget','approval'"
)
_SOURCE_KINDS_WITH_SYSTEM_PLAN = _SOURCE_KINDS_LEGACY + ",'system_plan_run'"


def _replace_source_kind_constraint(source_kinds: str) -> None:
    op.execute(
        "ALTER TABLE runtime_notification_outbox DROP CONSTRAINT IF EXISTS ck_runtime_notification_outbox_source_kind"
    )
    op.execute(
        "ALTER TABLE runtime_notification_outbox "
        "ADD CONSTRAINT ck_runtime_notification_outbox_source_kind "
        f"CHECK (source_kind IN ({source_kinds}))"
    )


def _runtime_notification_outbox_rls_state() -> tuple[bool, bool]:
    row = op.get_bind().execute(
        sa.text(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE oid = 'runtime_notification_outbox'::regclass
            """
        )
    ).one()
    return bool(row.relrowsecurity), bool(row.relforcerowsecurity)


def _restore_runtime_notification_outbox_rls(*, enabled: bool, forced: bool) -> None:
    if enabled:
        op.execute("ALTER TABLE runtime_notification_outbox ENABLE ROW LEVEL SECURITY")
    else:
        op.execute("ALTER TABLE runtime_notification_outbox DISABLE ROW LEVEL SECURITY")
    if forced:
        op.execute("ALTER TABLE runtime_notification_outbox FORCE ROW LEVEL SECURITY")
    else:
        op.execute("ALTER TABLE runtime_notification_outbox NO FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _replace_source_kind_constraint(_SOURCE_KINDS_WITH_SYSTEM_PLAN)


def downgrade() -> None:
    rls_enabled, rls_forced = _runtime_notification_outbox_rls_state()
    # The migration user may be the ordinary table owner and the table uses
    # FORCE RLS.  Temporarily disabling RLS is the only deterministic way for
    # that owner to remove every incompatible row.  Alembic's transaction
    # restores the original DDL on failure; the success path restores the
    # exact previous ENABLE/FORCE state below.
    op.execute("ALTER TABLE runtime_notification_outbox DISABLE ROW LEVEL SECURITY")
    op.execute("DELETE FROM runtime_notification_outbox WHERE source_kind = 'system_plan_run'")
    _replace_source_kind_constraint(_SOURCE_KINDS_LEGACY)
    _restore_runtime_notification_outbox_rls(enabled=rls_enabled, forced=rls_forced)
