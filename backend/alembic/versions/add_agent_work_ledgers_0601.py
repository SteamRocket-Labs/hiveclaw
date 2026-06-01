"""Add agent_work_ledgers — scoped agent execution work ledger.

Revision ID: add_agent_work_ledgers_0601
Revises: add_agent_plan_recommendations_0531
Create Date: 2026-06-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "add_agent_work_ledgers_0601"
down_revision: Union[str, None] = "add_agent_plan_recommendations_0531"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    from sqlalchemy import text

    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("agent_work_ledgers"):
        op.create_table(
            "agent_work_ledgers",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column(
                "agent_id",
                UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column(
                "plan_id",
                UUID(as_uuid=True),
                sa.ForeignKey("agent_plan_requests.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("runtime_task_id", UUID(as_uuid=True), nullable=True),
            sa.Column("source", sa.String(length=50), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="running"),
            sa.Column("current_phase", sa.String(length=120), nullable=True),
            sa.Column("todo_items_json", JSONB(), nullable=False, server_default="[]"),
            sa.Column("findings_json", JSONB(), nullable=False, server_default="[]"),
            sa.Column("progress_json", JSONB(), nullable=False, server_default="[]"),
            sa.Column("failures_json", JSONB(), nullable=False, server_default="[]"),
            sa.Column("verification_json", JSONB(), nullable=False, server_default="[]"),
            sa.Column("open_questions_json", JSONB(), nullable=False, server_default="[]"),
            sa.Column("evidence_refs_json", JSONB(), nullable=False, server_default="[]"),
            sa.Column("sensitivity_level", sa.String(length=30), nullable=False, server_default="internal"),
            sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_updated_by", sa.String(length=50), nullable=False, server_default="runtime"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_work_ledgers_tenant_id ON agent_work_ledgers (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_work_ledgers_agent_id ON agent_work_ledgers (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_work_ledgers_plan_id ON agent_work_ledgers (plan_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_work_ledgers_runtime_task_id ON agent_work_ledgers (runtime_task_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_work_ledgers_agent_status ON agent_work_ledgers (agent_id, status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_work_ledgers_tenant_status ON agent_work_ledgers (tenant_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_work_ledgers_tenant_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_work_ledgers_agent_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_work_ledgers_runtime_task_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_work_ledgers_plan_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_work_ledgers_agent_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_work_ledgers_tenant_id")
    op.drop_table("agent_work_ledgers")
