"""Add the durable mixed-runtime root coverage ledger.

Revision ID: runtime_root_ledger_0716
Revises: session_v2_projection_epoch_0716
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_snapshots.runtime_root_ledger_contract_0716 import (
    build_session_event_contract_function_sql,
)
from migration_snapshots.session_v2_projection_epoch_contract_0716 import (
    build_session_event_contract_function_sql as build_previous_session_event_contract_function_sql,
)


revision = "runtime_root_ledger_0716"
down_revision = "session_v2_projection_epoch_0716"
branch_labels = None
depends_on = None


RUNTIME_ROOT_LEDGER_TABLES: tuple[str, ...] = ("runtime_root_items",)


def upgrade() -> None:
    op.create_table(
        "runtime_root_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        # Intentionally not an FK: the requested-set ledger may be committed
        # before the root RuntimeTask itself exists.  runtime_task_id below is
        # the admitted durable enqueue and remains referentially constrained.
        sa.Column("root_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "runtime_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("root_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("root_session_id", sa.String(length=512), nullable=True),
        sa.Column("intent_key", sa.String(length=300), nullable=False),
        sa.Column("work_type", sa.String(length=64), nullable=False),
        sa.Column("target_ref", sa.String(length=512), nullable=False),
        sa.Column(
            "path_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=40), server_default="requested", nullable=False),
        sa.Column("admission_disposition", sa.String(length=24), server_default="requested", nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=True),
        sa.Column("budget_reservation_key", sa.String(length=300), nullable=True),
        sa.Column("approval_ref", sa.String(length=512), nullable=True),
        sa.Column("child_session_id", sa.String(length=512), nullable=True),
        sa.Column(
            "result_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("recovery_claimed_by", sa.String(length=200), nullable=True),
        sa.Column("recovery_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_recovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('requested', 'waiting_approval', 'queued', 'running', 'completed', "
            "'failed', 'killed', 'skipped', 'cancelled', 'suspended', "
            "'needs_reconciliation', 'not_admitted')",
            name="ck_runtime_root_items_state",
        ),
        sa.CheckConstraint(
            "admission_disposition IN ('requested', 'admitted', 'deferred', 'not_admitted')",
            name="ck_runtime_root_items_admission_disposition",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "root_runtime_task_id",
            "intent_key",
            name="uq_runtime_root_items_root_intent",
        ),
        if_not_exists=True,
    )
    for column in (
        "tenant_id",
        "root_runtime_task_id",
        "parent_runtime_task_id",
        "runtime_task_id",
        "source_agent_id",
        "root_user_id",
        "root_session_id",
        "work_type",
        "state",
        "admission_disposition",
        "reason_code",
        "budget_reservation_key",
        "approval_ref",
        "child_session_id",
    ):
        op.create_index(
            op.f(f"ix_runtime_root_items_{column}"),
            "runtime_root_items",
            [column],
            unique=False,
            if_not_exists=True,
        )
    op.create_index(
        "ix_runtime_root_items_root_coverage",
        "runtime_root_items",
        ["tenant_id", "root_runtime_task_id", "admission_disposition", "state"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_runtime_root_items_team_recovery",
        "runtime_root_items",
        [
            "work_type",
            "state",
            "runtime_task_id",
            "next_recovery_at",
            "recovery_claim_expires_at",
        ],
        unique=False,
        if_not_exists=True,
    )

    if op.get_bind().dialect.name == "postgresql":
        predicate = (
            "current_setting('app.current_tenant_id', true) = 'BYPASS' "
            "OR runtime_root_items.tenant_id::text = current_setting('app.current_tenant_id', true)"
        )
        op.execute("ALTER TABLE runtime_root_items ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE runtime_root_items FORCE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS runtime_root_items_tenant_isolation ON runtime_root_items")
        op.execute(
            "CREATE POLICY runtime_root_items_tenant_isolation ON runtime_root_items "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
        # The durable enqueue now binds a RuntimeTask to its child-session
        # projection.  Reinstall the frozen trigger delta so that this exact
        # task may author legacy-compatible evidence on either endpoint.
        op.execute(build_session_event_contract_function_sql())
        op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_session_event_v2_contract() FROM PUBLIC")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(build_previous_session_event_contract_function_sql())
        op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_session_event_v2_contract() FROM PUBLIC")
        op.execute("DROP POLICY IF EXISTS runtime_root_items_tenant_isolation ON runtime_root_items")
        op.execute("ALTER TABLE runtime_root_items NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE runtime_root_items DISABLE ROW LEVEL SECURITY")
    op.drop_table("runtime_root_items", if_exists=True)
