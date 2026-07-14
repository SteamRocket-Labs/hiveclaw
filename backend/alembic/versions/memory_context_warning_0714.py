"""Correct degraded memory events from errors to retryable warnings.

Revision ID: memory_context_warning_0714
Revises: session_permission_semantics_0713
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op


revision = "memory_context_warning_0714"
down_revision = "session_permission_semantics_0713"
branch_labels = None
depends_on = None


def _upgrade_sql() -> str:
    return """
        UPDATE chat_transcript_events
        SET item_type = 'warning',
            item_status = 'succeeded',
            metadata_json = jsonb_set(
                COALESCE(metadata_json, '{}'::jsonb),
                '{status}',
                '"degraded"'::jsonb,
                true
            )
        WHERE event_type = 'memory_context_degraded'
          AND (
              item_type <> 'warning'
              OR item_status <> 'succeeded'
              OR COALESCE(metadata_json ->> 'status', '') <> 'degraded'
          )
    """


def _downgrade_sql() -> str:
    return """
        UPDATE chat_transcript_events
        SET item_type = 'error',
            item_status = 'failed',
            metadata_json = jsonb_set(
                COALESCE(metadata_json, '{}'::jsonb),
                '{status}',
                '"failed"'::jsonb,
                true
            )
        WHERE event_type = 'memory_context_degraded'
    """


def _set_rls(enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    op.execute(f"ALTER TABLE chat_transcript_events {action} ROW LEVEL SECURITY")
    if enabled:
        op.execute("ALTER TABLE chat_transcript_events FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _set_rls(False)
    op.execute(_upgrade_sql())
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
    op.execute(_downgrade_sql())
    _set_rls(True)
