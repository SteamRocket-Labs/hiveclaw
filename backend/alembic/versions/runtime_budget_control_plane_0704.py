"""Add runtime budget control-plane tables.

Revision ID: runtime_budget_control_plane_0704
Revises: rls_remaining_global_and_derived_tables_0703
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "runtime_budget_control_plane_0704"
down_revision = "rls_remaining_global_and_derived_tables_0703"
branch_labels = None
depends_on = None


_RUNTIME_BUDGET_TABLES = (
    "runtime_budget_policies",
    "runtime_budget_runs",
    "runtime_budget_events",
)


def _tenant_predicate(table: str) -> str:
    return f"""
        current_setting('app.current_tenant_id', true) = 'BYPASS'
        OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
        OR {table}.tenant_id IS NULL
    """


def _enable_rls(table: str) -> None:
    predicate = _tenant_predicate(table)
    policy_name = f"tenant_isolation_{table}"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
    op.execute(
        f"""
        CREATE POLICY {policy_name} ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
        """
    )


def upgrade() -> None:
    op.create_table(
        "runtime_budget_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("scope_type", sa.String(length=40), server_default="tenant_default", nullable=False),
        sa.Column("source", sa.String(length=80), nullable=True),
        sa.Column("profile", sa.String(length=80), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("enforcement_mode", sa.String(length=20), server_default="enforce", nullable=False),
        sa.Column("fail_mode", sa.String(length=20), server_default="fail_closed", nullable=False),
        sa.Column("max_tokens", sa.BigInteger(), nullable=True),
        sa.Column("max_cache_miss_tokens", sa.BigInteger(), nullable=True),
        sa.Column("max_subagents", sa.Integer(), nullable=True),
        sa.Column("max_delegations", sa.Integer(), nullable=True),
        sa.Column("max_background_tasks", sa.Integer(), nullable=True),
        sa.Column("max_continuation_wakes", sa.Integer(), nullable=True),
        sa.Column("max_provider_calls", sa.Integer(), nullable=True),
        sa.Column("default_child_token_reservation", sa.BigInteger(), server_default="50000", nullable=False),
        sa.Column("default_llm_call_token_reservation", sa.BigInteger(), server_default="50000", nullable=False),
        sa.Column("policy_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(op.f("ix_runtime_budget_policies_tenant_id"), "runtime_budget_policies", ["tenant_id"])
    op.create_index(op.f("ix_runtime_budget_policies_enabled"), "runtime_budget_policies", ["enabled"])
    op.create_index(op.f("ix_runtime_budget_policies_scope_type"), "runtime_budget_policies", ["scope_type"])
    op.create_index(op.f("ix_runtime_budget_policies_source"), "runtime_budget_policies", ["source"])
    op.create_index(op.f("ix_runtime_budget_policies_profile"), "runtime_budget_policies", ["profile"])
    op.create_index(op.f("ix_runtime_budget_policies_agent_id"), "runtime_budget_policies", ["agent_id"])
    op.create_index(op.f("ix_runtime_budget_policies_trigger_id"), "runtime_budget_policies", ["trigger_id"])

    op.create_table(
        "runtime_budget_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("policy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runtime_budget_policies.id"), nullable=True),
        sa.Column("root_run_kind", sa.String(length=60), nullable=False),
        sa.Column("root_run_key", sa.String(length=300), nullable=False),
        sa.Column("root_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("root_session_id", sa.String(length=512), nullable=True),
        sa.Column("root_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("root_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=True),
        sa.Column("profile", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("enforcement_mode", sa.String(length=20), server_default="enforce", nullable=False),
        sa.Column("fail_mode", sa.String(length=20), server_default="fail_closed", nullable=False),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("max_tokens", sa.BigInteger(), nullable=True),
        sa.Column("max_cache_miss_tokens", sa.BigInteger(), nullable=True),
        sa.Column("max_subagents", sa.Integer(), nullable=True),
        sa.Column("max_delegations", sa.Integer(), nullable=True),
        sa.Column("max_background_tasks", sa.Integer(), nullable=True),
        sa.Column("max_continuation_wakes", sa.Integer(), nullable=True),
        sa.Column("max_provider_calls", sa.Integer(), nullable=True),
        sa.Column("reserved_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("used_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reserved_cache_miss_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("used_cache_miss_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reserved_subagents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_subagents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_delegations", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_delegations", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_background_tasks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_background_tasks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_continuation_wakes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_continuation_wakes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_provider_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_provider_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "root_run_kind", "root_run_key", name="uq_runtime_budget_root_run"),
    )
    for column in (
        "tenant_id",
        "policy_id",
        "root_run_kind",
        "root_run_key",
        "root_runtime_task_id",
        "root_session_id",
        "root_agent_id",
        "root_user_id",
        "source",
        "profile",
        "status",
        "expires_at",
    ):
        op.create_index(op.f(f"ix_runtime_budget_runs_{column}"), "runtime_budget_runs", [column])

    op.create_table(
        "runtime_budget_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column(
            "budget_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_budget_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("reservation_key", sa.String(length=200), nullable=True),
        sa.Column("allowed", sa.Boolean(), nullable=True),
        sa.Column("would_deny", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("amounts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("budget_run_id", "reservation_key", "event_type", name="uq_runtime_budget_event_key_type"),
    )
    for column in ("tenant_id", "budget_run_id", "event_type", "reservation_key", "runtime_task_id"):
        op.create_index(op.f(f"ix_runtime_budget_events_{column}"), "runtime_budget_events", [column])

    op.add_column("runtime_tasks", sa.Column("budget_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("runtime_tasks", sa.Column("budget_reservation_key", sa.String(length=200), nullable=True))
    op.add_column("runtime_tasks", sa.Column("budget_admission_status", sa.String(length=40), nullable=True))
    op.add_column("runtime_tasks", sa.Column("budget_terminal_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_runtime_tasks_budget_run_id_runtime_budget_runs",
        "runtime_tasks",
        "runtime_budget_runs",
        ["budget_run_id"],
        ["id"],
    )
    op.create_index(op.f("ix_runtime_tasks_budget_run_id"), "runtime_tasks", ["budget_run_id"])
    op.create_index(op.f("ix_runtime_tasks_budget_reservation_key"), "runtime_tasks", ["budget_reservation_key"])
    op.create_index(op.f("ix_runtime_tasks_budget_admission_status"), "runtime_tasks", ["budget_admission_status"])

    op.add_column(
        "coordination_signals",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    for table in _RUNTIME_BUDGET_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_RUNTIME_BUDGET_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index(op.f("ix_runtime_tasks_budget_admission_status"), table_name="runtime_tasks")
    op.drop_index(op.f("ix_runtime_tasks_budget_reservation_key"), table_name="runtime_tasks")
    op.drop_index(op.f("ix_runtime_tasks_budget_run_id"), table_name="runtime_tasks")
    op.drop_constraint("fk_runtime_tasks_budget_run_id_runtime_budget_runs", "runtime_tasks", type_="foreignkey")
    op.drop_column("coordination_signals", "metadata")
    op.drop_column("runtime_tasks", "budget_terminal_reason")
    op.drop_column("runtime_tasks", "budget_admission_status")
    op.drop_column("runtime_tasks", "budget_reservation_key")
    op.drop_column("runtime_tasks", "budget_run_id")

    op.drop_table("runtime_budget_events")
    op.drop_table("runtime_budget_runs")
    op.drop_table("runtime_budget_policies")
