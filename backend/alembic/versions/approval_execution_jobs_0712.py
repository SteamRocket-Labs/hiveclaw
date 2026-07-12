"""Make approved tool execution a durable RuntimeTask handoff.

Revision ID: approval_execution_jobs_0712
Revises: budget_transition_outbox_0711
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "approval_execution_jobs_0712"
down_revision = "budget_transition_outbox_0711"
branch_labels = None
depends_on = None


_RUNTIME_TASK_TYPES = (
    "web_chat_turn",
    "goal_continuation",
    "team_member",
    "advanced_plan",
    "workflow",
    "delegation",
    "business_task",
    "subagent",
    "trigger",
    "heartbeat",
    "coordinator_worker",
    "harness_canary",
    "a2a_delegation",
    "approval_execution",
)
_LEGACY_RUNTIME_TASK_TYPES = _RUNTIME_TASK_TYPES[:-1]
_APPROVAL_EXECUTION_STATUSES = (
    "pending",
    "approved",
    "rejected",
    "queued",
    "executing",
    "succeeded",
    "failed",
    "needs_reconciliation",
    "needs_reapproval",
)
_LEGACY_APPROVAL_EXECUTION_STATUSES = tuple(status for status in _APPROVAL_EXECUTION_STATUSES if status != "queued")


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # Production tables FORCE RLS.  Release backfill is cross-tenant by
    # definition, so pin both historical and current audited bypass GUCs for
    # this migration transaction instead of silently seeing zero rows.
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute("ALTER TABLE approval_requests DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY")
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_RUNTIME_TASK_TYPES)})",
    )
    op.drop_constraint("ck_approval_requests_execution_status", "approval_requests", type_="check")
    op.create_check_constraint(
        "ck_approval_requests_execution_status",
        "approval_requests",
        f"execution_status IN ({_quoted(_APPROVAL_EXECUTION_STATUSES)})",
    )
    op.add_column(
        "approval_requests",
        sa.Column("execution_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_approval_requests_execution_task_id_runtime_tasks",
        "approval_requests",
        "runtime_tasks",
        ["execution_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_approval_requests_execution_task_id",
        "approval_requests",
        ["execution_task_id"],
    )

    # Historical decisions that can no longer prove authority or freshness are
    # never replayed. They remain inspectable and explicitly require a new
    # approval request.
    op.execute(
        sa.text(
            """
            UPDATE approval_requests
            SET execution_status = 'needs_reapproval',
                execution_receipt = (
                    COALESCE(execution_receipt::jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        'migration', 'approval_execution_jobs_0712',
                        'backfill_state', 'needs_reapproval',
                        'backfill_reason', CASE
                            WHEN expires_at IS NULL OR expires_at <= now() THEN 'expired'
                            WHEN tenant_id IS NULL THEN 'missing_tenant_authority'
                            ELSE 'incomplete_immutable_ticket'
                        END,
                        'automatic_replay', false
                    )
                )::json
            WHERE status = 'approved'
              AND execution_status = 'approved'
              AND consumed_at IS NULL
              AND tool_name IS NOT NULL
              AND (
                    expires_at IS NULL OR expires_at <= now()
                    OR tenant_id IS NULL
                    OR requested_by IS NULL
                    OR resolved_by IS NULL
                    OR normalized_arguments IS NULL
                    OR input_hash IS NULL
                    OR policy_snapshot_hash IS NULL
                    OR execution_envelope IS NULL
                    OR execution_envelope_hash IS NULL
                    OR decision_id IS NULL
              )
            """
        )
    )
    # Valid, fresh, unconsumed approvals receive one deterministic durable job.
    # ON CONFLICT makes the migration restart-safe if a deployment is retried.
    op.execute(
        sa.text(
            """
            INSERT INTO runtime_tasks (
                id,
                task_type,
                parent_agent_id,
                tenant_id,
                status,
                prompt,
                trace_id,
                parent_session_id,
                root_user_id,
                root_session_id,
                delegation_chain_json,
                depth,
                priority,
                root_idempotency_key,
                config_snapshot_hash,
                policy_snapshot_hash,
                metadata_json
            )
            SELECT
                gen_random_uuid(),
                'approval_execution',
                approval.agent_id,
                approval.tenant_id,
                'pending',
                'Execute approved action: ' || approval.action_type,
                'approval:' || approval.id::text,
                COALESCE(
                    NULLIF(approval.details ->> 'session_id', ''),
                    NULLIF(approval.details ->> 'conversation_id', '')
                ),
                approval.requested_by,
                COALESCE(
                    NULLIF(approval.details ->> 'session_id', ''),
                    NULLIF(approval.details ->> 'conversation_id', '')
                ),
                jsonb_build_array('agent:' || approval.agent_id::text),
                1,
                25,
                'approval-execution:' || approval.id::text,
                md5('approval-execution:' || approval.id::text || '-config')
                    || md5('approval-execution:' || approval.id::text || '-config-2'),
                COALESCE(
                    approval.policy_snapshot_hash,
                    md5('approval-execution:' || approval.id::text || '-policy')
                        || md5('approval-execution:' || approval.id::text || '-policy-2')
                ),
                jsonb_build_object(
                    'schema', 'approval_execution_job.v1',
                    'approval_id', approval.id::text,
                    'approved_by_user_id', approval.resolved_by::text,
                    'phase', 'queued',
                    'backfilled_by', 'approval_execution_jobs_0712',
                    'side_effect_risk', 'not_started',
                    'reconciliation_retry_allowed', true
                )
            FROM approval_requests AS approval
            WHERE approval.status = 'approved'
              AND approval.execution_status = 'approved'
              AND approval.consumed_at IS NULL
              AND approval.expires_at > now()
              AND approval.tenant_id IS NOT NULL
              AND approval.requested_by IS NOT NULL
              AND approval.resolved_by IS NOT NULL
              AND approval.tool_name IS NOT NULL
              AND approval.normalized_arguments IS NOT NULL
              AND approval.input_hash IS NOT NULL
              AND approval.policy_snapshot_hash IS NOT NULL
              AND approval.execution_envelope IS NOT NULL
              AND approval.execution_envelope_hash IS NOT NULL
              AND approval.decision_id IS NOT NULL
            ON CONFLICT (root_idempotency_key) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE approval_requests AS approval
            SET execution_task_id = task.id,
                execution_status = 'queued',
                execution_receipt = (
                    COALESCE(approval.execution_receipt::jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        'migration', 'approval_execution_jobs_0712',
                        'backfill_state', 'queued',
                        'execution_task_id', task.id::text,
                        'automatic_replay', true
                    )
                )::json
            FROM runtime_tasks AS task
            WHERE task.root_idempotency_key = 'approval-execution:' || approval.id::text
              AND approval.status = 'approved'
              AND approval.execution_status = 'approved'
              AND approval.consumed_at IS NULL
              AND approval.expires_at > now()
            """
        )
    )
    unresolved_safe_tickets = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT count(*)
            FROM approval_requests AS approval
            WHERE approval.status = 'approved'
              AND approval.execution_status = 'approved'
              AND approval.consumed_at IS NULL
              AND approval.expires_at > now()
              AND approval.tenant_id IS NOT NULL
              AND approval.requested_by IS NOT NULL
              AND approval.resolved_by IS NOT NULL
              AND approval.tool_name IS NOT NULL
              AND approval.normalized_arguments IS NOT NULL
              AND approval.input_hash IS NOT NULL
              AND approval.policy_snapshot_hash IS NOT NULL
              AND approval.execution_envelope IS NOT NULL
              AND approval.execution_envelope_hash IS NOT NULL
              AND approval.decision_id IS NOT NULL
            """
            )
        )
        .scalar_one()
    )
    if int(unresolved_safe_tickets or 0) != 0:
        raise RuntimeError("approval_execution_jobs_0712 left safe approved tickets without durable execution jobs")
    op.execute("ALTER TABLE approval_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE approval_requests FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute("ALTER TABLE approval_requests DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE approval_requests SET execution_status = 'approved' "
        "WHERE execution_status = 'queued' AND consumed_at IS NULL"
    )
    op.drop_constraint(
        "uq_approval_requests_execution_task_id",
        "approval_requests",
        type_="unique",
    )
    op.drop_constraint(
        "fk_approval_requests_execution_task_id_runtime_tasks",
        "approval_requests",
        type_="foreignkey",
    )
    op.drop_column("approval_requests", "execution_task_id")
    op.execute("DELETE FROM runtime_tasks WHERE task_type = 'approval_execution'")
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_LEGACY_RUNTIME_TASK_TYPES)})",
    )
    op.drop_constraint("ck_approval_requests_execution_status", "approval_requests", type_="check")
    op.create_check_constraint(
        "ck_approval_requests_execution_status",
        "approval_requests",
        f"execution_status IN ({_quoted(_LEGACY_APPROVAL_EXECUTION_STATUSES)})",
    )
    op.execute("ALTER TABLE approval_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE approval_requests FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY")
