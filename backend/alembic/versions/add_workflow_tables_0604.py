"""Add workflow journal + definition tables (§9 P1).

workflow_definitions / workflow_steps / workflow_leaf_calls / workflow_quotas
— all tenant-scoped with RLS ENABLEd **and FORCEd** from day one (P0 gap B:
the production connection is the table owner, so ENABLE-only policies are
inert; these new tables must not inherit that gap). Every accessor goes
through tenant_scoped_session.

Revision ID: add_workflow_tables_0604
Revises: add_mcp_server_records_0602
Create Date: 2026-06-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "add_workflow_tables_0604"
down_revision: Union[str, None] = "add_mcp_server_records_0602"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WORKFLOW_TABLES = ("workflow_definitions", "workflow_steps", "workflow_leaf_calls", "workflow_quotas")


def _table_exists(table: str) -> bool:
    from sqlalchemy import text

    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("workflow_definitions"):
        op.create_table(
            "workflow_definitions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("definition_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("definition_hash", sa.String(length=80), nullable=False),
            sa.Column("definition_json", JSONB(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("visibility_scope", sa.String(length=20), nullable=False, server_default="agent"),
            sa.Column("call_policy", JSONB(), nullable=True),
            sa.Column("owner_type", sa.String(length=20), nullable=False, server_default="user"),
            sa.Column("owner_id", UUID(as_uuid=True), nullable=True),
            sa.Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
            sa.Column("created_by_agent_id", UUID(as_uuid=True), nullable=True),
            sa.Column("promoted_from_run_id", UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "name", "definition_version", name="uq_workflow_definition_version"),
        )

    if not _table_exists("workflow_steps"):
        op.create_table(
            "workflow_steps",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column(
                "run_id",
                UUID(as_uuid=True),
                sa.ForeignKey("runtime_tasks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("step_id", sa.String(length=100), nullable=False),
            sa.Column("phase", sa.String(length=100), nullable=True),
            sa.Column("step_type", sa.String(length=30), nullable=False, server_default="agent_step"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("input_hash", sa.String(length=80), nullable=True),
            sa.Column("definition_hash", sa.String(length=80), nullable=True),
            sa.Column("result_ref", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("run_id", "step_id", name="uq_workflow_step_per_run"),
        )

    if not _table_exists("workflow_leaf_calls"):
        op.create_table(
            "workflow_leaf_calls",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column(
                "run_id",
                UUID(as_uuid=True),
                sa.ForeignKey("runtime_tasks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("step_id", sa.String(length=100), nullable=False),
            sa.Column("leaf_id", sa.String(length=200), nullable=False),
            sa.Column("input_hash", sa.String(length=80), nullable=True),
            sa.Column("definition_hash", sa.String(length=80), nullable=True),
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("result_ref", sa.Text(), nullable=True),
            sa.Column("token_usage", JSONB(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("run_id", "step_id", "leaf_id", name="uq_workflow_leaf_per_step"),
            sa.UniqueConstraint("run_id", "idempotency_key", name="uq_workflow_leaf_idempotency"),
        )

    if not _table_exists("workflow_quotas"):
        op.create_table(
            "workflow_quotas",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column(
                "run_id",
                UUID(as_uuid=True),
                sa.ForeignKey("runtime_tasks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("allocated_tokens", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("consumed_tokens", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("run_id", name="uq_workflow_quota_per_run"),
        )

    # Single-column indexes mirroring the models' index=True columns.
    for table in _WORKFLOW_TABLES:
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)")
    for table in ("workflow_steps", "workflow_leaf_calls", "workflow_quotas"):
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_run_id ON {table} (run_id)")
    for table in ("workflow_steps", "workflow_leaf_calls"):
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_status ON {table} (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_workflow_definitions_name ON workflow_definitions (name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_workflow_definitions_status ON workflow_definitions (status)")

    # RLS: ENABLE + FORCE + tenant policy (same shape as add_row_level_security,
    # plus FORCE so the table owner is also subject to the policy — P0 gap B).
    op.execute("SELECT set_config('app.current_tenant_id', '', false)")
    for table in _WORKFLOW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            DO $$ BEGIN
                CREATE POLICY tenant_isolation_{table} ON {table}
                    USING (
                        current_setting('app.current_tenant_id', true) = 'BYPASS'
                        OR tenant_id::text = current_setting('app.current_tenant_id', true)
                        OR tenant_id IS NULL
                    );
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
        """)


def downgrade() -> None:
    for table in reversed(_WORKFLOW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        if _table_exists(table):
            op.drop_table(table)
