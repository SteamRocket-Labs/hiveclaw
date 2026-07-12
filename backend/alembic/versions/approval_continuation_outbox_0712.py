"""Route approval results through the durable session continuation outbox.

Revision ID: approval_continuation_outbox_0712
Revises: hook_failure_modes_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op


revision = "approval_continuation_outbox_0712"
down_revision = "hook_failure_modes_0712"
branch_labels = None
depends_on = None


_SOURCE_KINDS_WITH_APPROVAL = (
    "'subagent','agent_team','workflow','trigger','delegation','a2a_delegation','runtime_budget','approval'"
)
_SOURCE_KINDS_LEGACY = "'subagent','agent_team','workflow','trigger','delegation','a2a_delegation','runtime_budget'"


_BACKFILL_OUTBOX_SQL = r"""
WITH candidates AS (
    SELECT
        rt.id AS task_id,
        rt.tenant_id,
        rt.parent_agent_id,
        rt.status AS task_status,
        COALESCE(rt.result_summary, 'Approval execution finished.') AS summary,
        COALESCE(rt.metadata_json::jsonb, '{}'::jsonb) AS task_metadata,
        ar.id AS approval_id,
        COALESCE(ar.tool_name, ar.action_type, 'approved_action') AS tool_name,
        cs.id AS session_id,
        cs.user_id
    FROM runtime_tasks AS rt
    JOIN approval_requests AS ar
      ON ar.execution_task_id = rt.id
     AND ar.tenant_id = rt.tenant_id
    JOIN chat_sessions AS cs
      ON cs.tenant_id = rt.tenant_id
     AND cs.agent_id = rt.parent_agent_id
     AND (
        cs.id::text = COALESCE(
            NULLIF(ar.details::jsonb ->> 'session_id', ''),
            NULLIF(ar.details::jsonb ->> 'conversation_id', ''),
            NULLIF(rt.parent_session_id, '')
        )
        OR cs.external_conv_id = COALESCE(
            NULLIF(ar.details::jsonb ->> 'session_id', ''),
            NULLIF(ar.details::jsonb ->> 'conversation_id', ''),
            NULLIF(rt.parent_session_id, '')
        )
     )
    WHERE rt.task_type = 'approval_execution'
      AND rt.status IN ('completed','failed','needs_reconciliation')
      AND rt.tenant_id IS NOT NULL
      AND rt.parent_agent_id IS NOT NULL
      AND cs.user_id IS NOT NULL
)
INSERT INTO runtime_notification_outbox (
    id,
    tenant_id,
    source_kind,
    source_run_id,
    parent_session_id,
    parent_agent_id,
    parent_user_id,
    terminal_status,
    task_type,
    summary,
    delivery_mode,
    artifacts_json,
    metadata_json,
    payload_rank,
    status,
    attempt_count,
    available_at,
    created_at,
    updated_at
)
SELECT
    (
        substr(md5('approval-continuation:' || candidates.task_id::text || ':' || candidates.session_id::text), 1, 8)
        || '-' || substr(md5('approval-continuation:' || candidates.task_id::text || ':' || candidates.session_id::text), 9, 4)
        || '-4' || substr(md5('approval-continuation:' || candidates.task_id::text || ':' || candidates.session_id::text), 14, 3)
        || '-a' || substr(md5('approval-continuation:' || candidates.task_id::text || ':' || candidates.session_id::text), 18, 3)
        || '-' || substr(md5('approval-continuation:' || candidates.task_id::text || ':' || candidates.session_id::text), 21, 12)
    )::uuid,
    candidates.tenant_id,
    'approval',
    candidates.task_id::text,
    candidates.session_id,
    candidates.parent_agent_id,
    candidates.user_id,
    CASE
        WHEN candidates.task_status = 'completed' THEN 'completed'
        WHEN candidates.task_status = 'needs_reconciliation' THEN 'needs_reconciliation'
        ELSE 'failed'
    END,
    'approval_execution',
    candidates.summary,
    'parent_continuation',
    '[]'::jsonb,
    candidates.task_metadata || jsonb_build_object(
        'approval_id', candidates.approval_id::text,
        'tool_name', candidates.tool_name,
        'model_context', '[Approval tool result]' || chr(10)
            || 'Approval: ' || candidates.approval_id::text || chr(10)
            || 'Tool: ' || candidates.tool_name || chr(10)
            || 'Result: ' || candidates.summary || chr(10)
            || 'Continue the original task from this approved tool result.',
        'reconciled_from_legacy_approval_execution', true
    ),
    10,
    'pending',
    0,
    now(),
    now(),
    now()
FROM candidates
ON CONFLICT ON CONSTRAINT uq_runtime_notification_outbox_delivery DO NOTHING;
"""

_BACKFILL_RECEIPT_SQL = r"""
UPDATE approval_requests AS ar
SET execution_receipt = (
    COALESCE(ar.execution_receipt::jsonb, '{}'::jsonb)
    || jsonb_build_object(
        'continuation_status', CASE WHEN outbox.status = 'pending' THEN 'queued' ELSE outbox.status END,
        'continuation_outbox_id', outbox.id::text,
        'origin_session_id', outbox.parent_session_id::text
    )
)::json
FROM runtime_notification_outbox AS outbox
WHERE outbox.source_kind = 'approval'
  AND outbox.metadata_json ->> 'approval_id' = ar.id::text;
"""


def _replace_source_kind_constraint(source_kinds: str) -> None:
    op.execute(
        "ALTER TABLE runtime_notification_outbox DROP CONSTRAINT IF EXISTS ck_runtime_notification_outbox_source_kind"
    )
    op.execute(
        "ALTER TABLE runtime_notification_outbox "
        "ADD CONSTRAINT ck_runtime_notification_outbox_source_kind "
        f"CHECK (source_kind IN ({source_kinds}))"
    )


def upgrade() -> None:
    _replace_source_kind_constraint(_SOURCE_KINDS_WITH_APPROVAL)
    op.execute(_BACKFILL_OUTBOX_SQL)
    op.execute(_BACKFILL_RECEIPT_SQL)


def downgrade() -> None:
    op.execute("DELETE FROM runtime_notification_outbox WHERE source_kind = 'approval'")
    op.execute(
        """
        UPDATE approval_requests
        SET execution_receipt = (
            COALESCE(execution_receipt::jsonb, '{}'::jsonb)
            - 'continuation_status'
            - 'continuation_outbox_id'
            - 'continuation_attempt_count'
            - 'continuation_error'
            - 'continuation_reason'
            - 'origin_session_id'
        )::json
        WHERE COALESCE(execution_receipt::jsonb, '{}'::jsonb) ? 'continuation_status'
        """
    )
    _replace_source_kind_constraint(_SOURCE_KINDS_LEGACY)
