"""Canonicalize durable completion source-kind identities.

Revision ID: runtime_notification_source_kind_0713
Revises: runtime_notification_heartbeat_0713
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "runtime_notification_source_kind_0713"
down_revision = "runtime_notification_heartbeat_0713"
branch_labels = None
depends_on = None

_SOURCE_KINDS_CANONICAL = (
    "'subagent','agent_team','workflow','trigger','heartbeat','a2a_delegation',"
    "'runtime_budget','approval','system_plan_run'"
)
_SOURCE_KINDS_WITH_LEGACY_DELEGATION = (
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
    enabled, forced = _rls_state()
    op.execute("ALTER TABLE runtime_notification_outbox DISABLE ROW LEVEL SECURITY")

    # Two live claims, or one delivered authority plus one in-flight claim,
    # prove that the duplicate identities have already crossed a delivery
    # boundary.  Choosing either row would silently hide a possible double
    # continuation, so stop for explicit operator reconciliation.
    dual_processing = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT canonical.id
            FROM runtime_notification_outbox AS canonical
            JOIN runtime_notification_outbox AS legacy
              ON canonical.tenant_id = legacy.tenant_id
             AND canonical.source_run_id = legacy.source_run_id
             AND canonical.parent_session_id = legacy.parent_session_id
             AND canonical.terminal_status = legacy.terminal_status
            WHERE canonical.source_kind = 'a2a_delegation'
              AND legacy.source_kind = 'delegation'
              AND canonical.status = 'processing'
              AND legacy.status = 'processing'
            LIMIT 1
            """
            )
        )
        .first()
    )
    if dual_processing is not None:
        raise RuntimeError("dual_processing_completion_collision")

    delivered_processing = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT canonical.id
            FROM runtime_notification_outbox AS canonical
            JOIN runtime_notification_outbox AS legacy
              ON canonical.tenant_id = legacy.tenant_id
             AND canonical.source_run_id = legacy.source_run_id
             AND canonical.parent_session_id = legacy.parent_session_id
             AND canonical.terminal_status = legacy.terminal_status
            WHERE canonical.source_kind = 'a2a_delegation'
              AND legacy.source_kind = 'delegation'
              AND (
                    (canonical.status = 'delivered' AND legacy.status = 'processing')
                 OR (canonical.status = 'processing' AND legacy.status = 'delivered')
              )
            LIMIT 1
            """
            )
        )
        .first()
    )
    if delivered_processing is not None:
        raise RuntimeError("delivered_processing_completion_collision")

    # When only the legacy identity owns the strongest completed/in-flight
    # authority, retain that row ID.  This is required for a pre-upgrade worker
    # to ACK the same claim epoch after the migration.  Payload fields may be
    # enriched by rank, but status, receipt, worker, lease and attempt epoch
    # stay with the authority row.
    op.execute(
        """
        UPDATE runtime_notification_outbox AS legacy
        SET
            child_session_id = CASE
                WHEN canonical.payload_rank > legacy.payload_rank THEN canonical.child_session_id
                ELSE legacy.child_session_id
            END,
            child_agent_name = CASE
                WHEN canonical.payload_rank > legacy.payload_rank THEN canonical.child_agent_name
                ELSE legacy.child_agent_name
            END,
            task_type = 'delegation',
            summary = CASE
                WHEN canonical.payload_rank > legacy.payload_rank THEN canonical.summary
                ELSE legacy.summary
            END,
            delivery_mode = CASE
                WHEN canonical.payload_rank > legacy.payload_rank THEN canonical.delivery_mode
                ELSE legacy.delivery_mode
            END,
            artifacts_json = CASE
                WHEN canonical.payload_rank > legacy.payload_rank THEN canonical.artifacts_json
                ELSE legacy.artifacts_json
            END,
            metadata_json = CASE
                WHEN canonical.payload_rank > legacy.payload_rank THEN canonical.metadata_json
                ELSE legacy.metadata_json
            END,
            payload_rank = GREATEST(legacy.payload_rank, canonical.payload_rank),
            last_error = COALESCE(legacy.last_error, canonical.last_error),
            updated_at = GREATEST(legacy.updated_at, canonical.updated_at)
        FROM runtime_notification_outbox AS canonical
        WHERE legacy.source_kind = 'delegation'
          AND canonical.source_kind = 'a2a_delegation'
          AND canonical.tenant_id = legacy.tenant_id
          AND canonical.source_run_id = legacy.source_run_id
          AND canonical.parent_session_id = legacy.parent_session_id
          AND canonical.terminal_status = legacy.terminal_status
          AND (
                (legacy.status = 'delivered' AND canonical.status NOT IN ('delivered', 'processing'))
             OR (legacy.status = 'processing' AND canonical.status NOT IN ('delivered', 'processing'))
          )
        """
    )
    op.execute(
        """
        DELETE FROM runtime_notification_outbox AS canonical
        USING runtime_notification_outbox AS legacy
        WHERE legacy.source_kind = 'delegation'
          AND canonical.source_kind = 'a2a_delegation'
          AND canonical.tenant_id = legacy.tenant_id
          AND canonical.source_run_id = legacy.source_run_id
          AND canonical.parent_session_id = legacy.parent_session_id
          AND canonical.terminal_status = legacy.terminal_status
          AND (
                (legacy.status = 'delivered' AND canonical.status NOT IN ('delivered', 'processing'))
             OR (legacy.status = 'processing' AND canonical.status NOT IN ('delivered', 'processing'))
          )
        """
    )

    # Remaining collisions are canonically owned. Merge only the highest-ranked
    # payload into that row; an existing processing/delivered authority retains
    # its claim epoch or delivery receipt unchanged.
    op.execute(
        """
        UPDATE runtime_notification_outbox AS canonical
        SET
            child_session_id = CASE
                WHEN legacy.payload_rank > canonical.payload_rank THEN legacy.child_session_id
                ELSE canonical.child_session_id
            END,
            child_agent_name = CASE
                WHEN legacy.payload_rank > canonical.payload_rank THEN legacy.child_agent_name
                ELSE canonical.child_agent_name
            END,
            task_type = 'delegation',
            summary = CASE
                WHEN legacy.payload_rank > canonical.payload_rank THEN legacy.summary
                ELSE canonical.summary
            END,
            delivery_mode = CASE
                WHEN legacy.payload_rank > canonical.payload_rank THEN legacy.delivery_mode
                ELSE canonical.delivery_mode
            END,
            artifacts_json = CASE
                WHEN legacy.payload_rank > canonical.payload_rank THEN legacy.artifacts_json
                ELSE canonical.artifacts_json
            END,
            metadata_json = CASE
                WHEN legacy.payload_rank > canonical.payload_rank THEN legacy.metadata_json
                ELSE canonical.metadata_json
            END,
            payload_rank = GREATEST(legacy.payload_rank, canonical.payload_rank),
            status = CASE
                WHEN legacy.status = 'delivered' OR canonical.status = 'delivered' THEN 'delivered'
                WHEN legacy.status = 'processing' OR canonical.status = 'processing' THEN 'processing'
                WHEN legacy.status = 'pending' OR canonical.status = 'pending' THEN 'pending'
                ELSE 'dead_letter'
            END,
            attempt_count = CASE
                WHEN canonical.status IN ('processing', 'delivered') THEN canonical.attempt_count
                ELSE GREATEST(legacy.attempt_count, canonical.attempt_count)
            END,
            available_at = CASE
                WHEN canonical.status IN ('processing', 'delivered') THEN canonical.available_at
                ELSE LEAST(legacy.available_at, canonical.available_at)
            END,
            locked_by = CASE
                WHEN canonical.status = 'processing' THEN canonical.locked_by
                ELSE NULL
            END,
            locked_at = CASE
                WHEN canonical.status = 'processing' THEN canonical.locked_at
                ELSE NULL
            END,
            last_error = COALESCE(canonical.last_error, legacy.last_error),
            delivery_receipt_json = CASE
                WHEN canonical.status = 'delivered' THEN canonical.delivery_receipt_json
                ELSE COALESCE(canonical.delivery_receipt_json, legacy.delivery_receipt_json)
            END,
            delivered_at = CASE
                WHEN canonical.status = 'delivered' THEN canonical.delivered_at
                ELSE NULL
            END,
            updated_at = GREATEST(legacy.updated_at, canonical.updated_at)
        FROM runtime_notification_outbox AS legacy
        WHERE legacy.source_kind = 'delegation'
          AND canonical.source_kind = 'a2a_delegation'
          AND canonical.tenant_id = legacy.tenant_id
          AND canonical.source_run_id = legacy.source_run_id
          AND canonical.parent_session_id = legacy.parent_session_id
          AND canonical.terminal_status = legacy.terminal_status
        """
    )
    op.execute(
        """
        DELETE FROM runtime_notification_outbox AS legacy
        USING runtime_notification_outbox AS canonical
        WHERE legacy.source_kind = 'delegation'
          AND canonical.source_kind = 'a2a_delegation'
          AND canonical.tenant_id = legacy.tenant_id
          AND canonical.source_run_id = legacy.source_run_id
          AND canonical.parent_session_id = legacy.parent_session_id
          AND canonical.terminal_status = legacy.terminal_status
        """
    )
    op.execute(
        "UPDATE runtime_notification_outbox "
        "SET source_kind = 'a2a_delegation', task_type = 'delegation', updated_at = now() "
        "WHERE source_kind = 'delegation'"
    )
    _replace_source_kind_constraint(_SOURCE_KINDS_CANONICAL)
    _restore_rls_state(enabled=enabled, forced=forced)


def downgrade() -> None:
    enabled, forced = _rls_state()
    op.execute("ALTER TABLE runtime_notification_outbox DISABLE ROW LEVEL SECURITY")
    # Canonicalization is intentionally not reversed: one canonical row cannot
    # be split back into two historical intents without manufacturing a second
    # delivery. Downgrade restores only compatibility with legacy writers.
    _replace_source_kind_constraint(_SOURCE_KINDS_WITH_LEGACY_DELEGATION)
    _restore_rls_state(enabled=enabled, forced=forced)
