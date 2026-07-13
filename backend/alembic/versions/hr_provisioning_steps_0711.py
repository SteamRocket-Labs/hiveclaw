"""Add fenced, replayable HR provisioning steps.

Revision ID: hr_provisioning_steps_0711
Revises: runtime_task_root_authority_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import add_column_if_missing, create_index_if_missing, create_table_if_missing


revision = "hr_provisioning_steps_0711"
down_revision = "runtime_task_root_authority_0711"
branch_labels = None
depends_on = None

_HR_PROVISIONING_TABLES = ("hr_provisioning_steps",)


def upgrade() -> None:
    add_column_if_missing(
        op, "hr_creation_drafts", sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True)
    )
    add_column_if_missing(
        op,
        "hr_creation_drafts",
        sa.Column("claim_version", sa.Integer(), server_default="0", nullable=False),
    )
    add_column_if_missing(
        op,
        "hr_creation_drafts",
        sa.Column("claim_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Deploying a fencing token makes every pre-fencing lease unverifiable.
    # Clear only the lease, preserving the durable draft/provisioning status so
    # the next worker can safely reclaim and resume it.
    op.execute("UPDATE hr_creation_drafts SET claim_expires_at = NULL WHERE claim_expires_at IS NOT NULL")

    create_table_if_missing(
        op,
        "hr_provisioning_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hr_creation_drafts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_key", sa.String(length=160), nullable=False),
        sa.Column("step_kind", sa.String(length=64), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("input_hash", sa.String(length=80), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_version", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "draft_id", "step_key", name="uq_hr_provisioning_step_key"),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','skipped','waiting_review')",
            name="ck_hr_provisioning_step_status",
        ),
    )
    create_index_if_missing(
        op,
        "ix_hr_provisioning_steps_draft_order",
        "hr_provisioning_steps",
        ["draft_id", "order_index"],
    )
    create_index_if_missing(
        op,
        "ix_hr_provisioning_steps_tenant_status",
        "hr_provisioning_steps",
        ["tenant_id", "status"],
    )

    # Base-step backfill keeps old in-flight drafts resumable. Completed drafts
    # retain their historical terminal state with an explicit legacy receipt;
    # they are not silently reclassified as newly verified executions.
    op.execute(
        r"""
        INSERT INTO hr_provisioning_steps (
            id, tenant_id, draft_id, step_key, step_kind, order_index, required,
            status, input_hash, attempt_count, completed_at, receipt_json
        )
        SELECT
            gen_random_uuid(), draft.tenant_id, draft.id, spec.step_key, spec.step_kind,
            spec.order_index, TRUE,
            CASE
                WHEN draft.status = 'completed' THEN 'completed'
                WHEN spec.step_key = 'validate' AND draft.status IN ('confirmed','creating','provisioning','failed')
                    THEN 'completed'
                WHEN spec.step_key IN ('model','core') AND draft.created_agent_id IS NOT NULL THEN 'completed'
                WHEN spec.step_key = 'workspace' AND draft.provisioning_json ->> 'workspace' = 'completed'
                    THEN 'completed'
                WHEN spec.step_key = 'defaults' AND draft.provisioning_json ->> 'default_skills' = 'completed'
                    THEN 'completed'
                WHEN spec.step_key = 't0_evidence' AND draft.provisioning_json ->> 't0_evidence' = 'completed'
                    THEN 'completed'
                ELSE 'pending'
            END,
            'legacy:' || draft.blueprint_hash || ':' || spec.step_key,
            CASE WHEN draft.status = 'completed' THEN 1 ELSE 0 END,
            CASE WHEN draft.status = 'completed' THEN COALESCE(draft.updated_at, now()) ELSE NULL END,
            CASE
                WHEN draft.status = 'completed' THEN '{"backfill":"legacy_completed_draft"}'::jsonb
                ELSE '{"backfill":"legacy_resumable_draft"}'::jsonb
            END
        FROM hr_creation_drafts AS draft
        CROSS JOIN (VALUES
            ('validate', 'validate', 10),
            ('model', 'model', 20),
            ('core', 'core', 30),
            ('workspace', 'workspace', 40),
            ('defaults', 'defaults', 50),
            ('t0_evidence', 't0_evidence', 60),
            ('finalize', 'finalize', 10000)
        ) AS spec(step_key, step_kind, order_index)
        WHERE draft.status IN ('confirmed','creating','provisioning','completed','failed')
        """
    )

    # Canonical blueprints may contain day-one capabilities. Expand every array
    # into its own required step so an existing Agent row can never erase the
    # remaining install plan after a restart.
    for json_key, kind, order_offset in (
        ("effective_skill_names", "platform_skill", 100),
        ("skill_names", "platform_skill", 500),
        ("mcp_server_ids", "mcp_server", 1000),
        ("clawhub_slugs", "clawhub_skill", 2000),
        ("external_skill_refs", "external_skill_url", 3000),
        ("external_skill_urls", "external_skill_url", 3500),
    ):
        op.execute(
            sa.text(
                f"""
                INSERT INTO hr_provisioning_steps (
                    id, tenant_id, draft_id, step_key, step_kind, order_index, required,
                    status, input_hash, source_key, attempt_count, completed_at, receipt_json
                )
                SELECT
                    gen_random_uuid(), draft.tenant_id, draft.id,
                    'capability:{kind}:' || md5(lower(trim(item.value))),
                    '{kind}', {order_offset} + item.ordinality::integer, TRUE,
                    CASE WHEN draft.status = 'completed' THEN 'completed' ELSE 'pending' END,
                    'legacy:' || draft.blueprint_hash || ':{kind}:' || md5(lower(trim(item.value))),
                    item.value,
                    CASE WHEN draft.status = 'completed' THEN 1 ELSE 0 END,
                    CASE WHEN draft.status = 'completed' THEN COALESCE(draft.updated_at, now()) ELSE NULL END,
                    CASE
                        WHEN draft.status = 'completed' THEN '{{"backfill":"legacy_completed_draft"}}'::jsonb
                        ELSE '{{"backfill":"legacy_resumable_draft"}}'::jsonb
                    END
                FROM hr_creation_drafts AS draft
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    COALESCE(draft.blueprint_json -> '{json_key}', '[]'::jsonb)
                ) WITH ORDINALITY AS item(value, ordinality)
                WHERE draft.status IN ('confirmed','creating','provisioning','completed','failed')
                  AND trim(item.value) <> ''
                ON CONFLICT (tenant_id, draft_id, step_key) DO NOTHING
                """
            )
        )

    op.execute("ALTER TABLE hr_provisioning_steps ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hr_provisioning_steps FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_hr_provisioning_steps ON hr_provisioning_steps")
    op.execute(
        """
        CREATE POLICY tenant_isolation_hr_provisioning_steps ON hr_provisioning_steps
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_hr_provisioning_steps_tenant_status", table_name="hr_provisioning_steps")
    op.drop_index("ix_hr_provisioning_steps_draft_order", table_name="hr_provisioning_steps")
    op.drop_table("hr_provisioning_steps")
    op.drop_column("hr_creation_drafts", "claim_heartbeat_at")
    op.drop_column("hr_creation_drafts", "claim_version")
    op.drop_column("hr_creation_drafts", "claim_token")
