"""Allow durable heartbeat reconciliation notifications.

Revision ID: runtime_notification_heartbeat_0713
Revises: runtime_task_claim_lanes_0713
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "runtime_notification_heartbeat_0713"
down_revision = "runtime_task_claim_lanes_0713"
branch_labels = None
depends_on = None

_SOURCE_KINDS_PREVIOUS = (
    "'subagent','agent_team','workflow','trigger','delegation','a2a_delegation',"
    "'runtime_budget','approval','system_plan_run'"
)
_SOURCE_KINDS_WITH_HEARTBEAT = (
    "'subagent','agent_team','workflow','trigger','heartbeat','delegation','a2a_delegation',"
    "'runtime_budget','approval','system_plan_run'"
)


def _replace_source_kind_constraint(source_kinds: str) -> None:
    op.execute(
        "ALTER TABLE runtime_notification_outbox DROP CONSTRAINT IF EXISTS ck_runtime_notification_outbox_source_kind"
    )
    op.execute(
        "ALTER TABLE runtime_notification_outbox "
        "ADD CONSTRAINT ck_runtime_notification_outbox_source_kind "
        f"CHECK (source_kind IN ({source_kinds}))"
    )


def _rls_state() -> tuple[bool, bool]:
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'runtime_notification_outbox'::regclass"
            )
        )
        .one()
    )
    return bool(row.relrowsecurity), bool(row.relforcerowsecurity)


def _restore_rls_state(*, enabled: bool, forced: bool) -> None:
    op.execute(
        "ALTER TABLE runtime_notification_outbox " + ("ENABLE" if enabled else "DISABLE") + " ROW LEVEL SECURITY"
    )
    op.execute("ALTER TABLE runtime_notification_outbox " + ("FORCE" if forced else "NO FORCE") + " ROW LEVEL SECURITY")


def upgrade() -> None:
    _replace_source_kind_constraint(_SOURCE_KINDS_WITH_HEARTBEAT)


def downgrade() -> None:
    rls_enabled, rls_forced = _rls_state()
    op.execute("ALTER TABLE runtime_notification_outbox DISABLE ROW LEVEL SECURITY")
    op.execute("DELETE FROM runtime_notification_outbox WHERE source_kind = 'heartbeat'")
    _replace_source_kind_constraint(_SOURCE_KINDS_PREVIOUS)
    _restore_rls_state(enabled=rls_enabled, forced=rls_forced)
