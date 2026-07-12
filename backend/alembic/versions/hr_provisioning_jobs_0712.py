"""Make confirmed HR blueprints durable worker jobs.

Revision ID: hr_provisioning_jobs_0712
Revises: approval_execution_jobs_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "hr_provisioning_jobs_0712"
down_revision = "approval_execution_jobs_0712"
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
    "hr_provisioning",
)
_LEGACY_RUNTIME_TASK_TYPES = _RUNTIME_TASK_TYPES[:-1]


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute("ALTER TABLE hr_creation_drafts DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY")

    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_RUNTIME_TASK_TYPES)})",
    )
    op.add_column(
        "hr_creation_drafts",
        sa.Column("provisioning_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_hr_creation_drafts_provisioning_task_id_runtime_tasks",
        "hr_creation_drafts",
        "runtime_tasks",
        ["provisioning_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_hr_creation_drafts_provisioning_task_id",
        "hr_creation_drafts",
        ["provisioning_task_id"],
    )

    # Every legacy non-terminal confirmed decision receives one job. The exact
    # predicate is intentionally visible to the structural acceptance test:
    # status IN ('confirmed','creating','provisioning','failed')
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
                scheduled_at,
                completed_at,
                priority,
                root_idempotency_key,
                config_snapshot_hash,
                policy_snapshot_hash,
                metadata_json
            )
            SELECT
                gen_random_uuid(),
                'hr_provisioning',
                draft.hr_agent_id,
                draft.tenant_id,
                CASE
                    WHEN draft.status = 'confirmed' THEN 'pending'
                    WHEN draft.status IN ('creating', 'provisioning') THEN 'resumable'
                    ELSE 'failed'
                END,
                'Provision the authenticated canonical HR blueprint.',
                'hr-provisioning:' || draft.id::text,
                draft.session_id::text,
                draft.requested_by_user_id,
                draft.session_id::text,
                jsonb_build_array('agent:' || draft.hr_agent_id::text),
                1,
                CASE
                    WHEN draft.status IN ('creating', 'provisioning')
                        THEN COALESCE(draft.claim_expires_at, now())
                    WHEN draft.status = 'confirmed' THEN now()
                    ELSE NULL
                END,
                CASE WHEN draft.status = 'failed' THEN now() ELSE NULL END,
                24,
                'hr-provisioning:' || draft.id::text || '-v' || draft.blueprint_version::text,
                md5('hr-provisioning:' || draft.id::text || '-config')
                    || md5('hr-provisioning:' || draft.id::text || '-config-2'),
                md5('hr-provisioning:' || draft.id::text || '-policy')
                    || md5('hr-provisioning:' || draft.id::text || '-policy-2'),
                jsonb_build_object(
                    'schema', 'hr_provisioning_job.v1',
                    'draft_id', draft.id::text,
                    'blueprint_version', draft.blueprint_version,
                    'blueprint_hash', draft.blueprint_hash,
                    'phase', CASE
                        WHEN draft.status = 'confirmed' THEN 'queued'
                        WHEN draft.status IN ('creating', 'provisioning') THEN 'waiting_draft_claim_expiry'
                        ELSE 'terminal'
                    END,
                    'backfilled_by', 'hr_provisioning_jobs_0712',
                    'side_effect_risk', CASE
                        WHEN draft.status = 'confirmed' THEN 'not_started'
                        ELSE 'journaled_or_in_progress'
                    END,
                    'automatic_retry_allowed', true
                )
            FROM hr_creation_drafts AS draft
            WHERE draft.status IN ('confirmed','creating','provisioning','failed')
              AND draft.confirmed_by_user_id IS NOT NULL
              AND draft.confirmed_at IS NOT NULL
            ON CONFLICT (root_idempotency_key) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE hr_creation_drafts AS draft
            SET provisioning_task_id = task.id,
                provisioning_json = COALESCE(draft.provisioning_json, '{}'::jsonb)
                    || jsonb_build_object(
                        'runtime_task_id', task.id::text,
                        'runtime_status', task.status,
                        'runtime_phase', task.metadata_json ->> 'phase',
                        'backfilled_by', 'hr_provisioning_jobs_0712'
                    )
            FROM runtime_tasks AS task
            WHERE task.root_idempotency_key =
                    'hr-provisioning:' || draft.id::text || '-v' || draft.blueprint_version::text
              AND task.task_type = 'hr_provisioning'
              AND draft.status IN ('confirmed','creating','provisioning','failed')
            """
        )
    )
    unresolved = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT count(*)
                FROM hr_creation_drafts
                WHERE status IN ('confirmed','creating','provisioning','failed')
                  AND confirmed_by_user_id IS NOT NULL
                  AND confirmed_at IS NOT NULL
                  AND provisioning_task_id IS NULL
                """
            )
        )
        .scalar_one()
    )
    if int(unresolved or 0) != 0:
        raise RuntimeError("hr_provisioning_jobs_0712 left confirmed HR drafts without durable jobs")

    op.execute("ALTER TABLE hr_creation_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hr_creation_drafts FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute("ALTER TABLE hr_creation_drafts DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY")
    op.execute("UPDATE hr_creation_drafts SET provisioning_task_id = NULL")
    op.drop_constraint(
        "uq_hr_creation_drafts_provisioning_task_id",
        "hr_creation_drafts",
        type_="unique",
    )
    op.drop_constraint(
        "fk_hr_creation_drafts_provisioning_task_id_runtime_tasks",
        "hr_creation_drafts",
        type_="foreignkey",
    )
    op.drop_column("hr_creation_drafts", "provisioning_task_id")
    op.execute("DELETE FROM runtime_tasks WHERE task_type = 'hr_provisioning'")
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_LEGACY_RUNTIME_TASK_TYPES)})",
    )
    op.execute("ALTER TABLE hr_creation_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hr_creation_drafts FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY")
