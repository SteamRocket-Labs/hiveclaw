"""Add runtime task persistence and agent execution_mode.

Revision ID: add_runtime_tasks_exec_mode_0401
Revises: update_tenant_tz_0331
Create Date: 2026-04-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, UUID


revision: str = "add_runtime_tasks_exec_mode_0401"
down_revision: Union[str, None] = "update_tenant_tz_0331"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    from sqlalchemy import text
    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def _table_exists(table: str) -> bool:
    from sqlalchemy import text
    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _column_exists("agents", "execution_mode"):
        op.add_column(
            "agents",
            sa.Column("execution_mode", sa.String(length=30), nullable=False, server_default="standard"),
        )

    if not _table_exists("runtime_tasks"):
        op.create_table(
            "runtime_tasks",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("task_type", sa.String(length=50), nullable=False, server_default="delegation"),
            sa.Column("parent_agent_id", UUID(as_uuid=True), nullable=True),
            sa.Column("child_agent_id", UUID(as_uuid=True), nullable=True),
            sa.Column("child_agent_name", sa.String(length=200), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("prompt", sa.Text(), nullable=True),
            sa.Column("result_summary", sa.Text(), nullable=True),
            sa.Column("token_usage", JSON(), nullable=True),
            sa.Column("trace_id", sa.String(length=255), nullable=True),
            sa.Column("parent_session_id", sa.String(length=100), nullable=True),
            sa.Column("child_session_id", sa.String(length=100), nullable=True),
            sa.Column("depth", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", JSON(), nullable=True),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_runtime_tasks_parent_agent_id ON runtime_tasks (parent_agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runtime_tasks_child_agent_id ON runtime_tasks (child_agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runtime_tasks_status ON runtime_tasks (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runtime_tasks_trace_id ON runtime_tasks (trace_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runtime_tasks_created_at ON runtime_tasks (created_at)")


def downgrade() -> None:
    op.drop_index("ix_runtime_tasks_created_at", table_name="runtime_tasks")
    op.drop_index("ix_runtime_tasks_trace_id", table_name="runtime_tasks")
    op.drop_index("ix_runtime_tasks_status", table_name="runtime_tasks")
    op.drop_index("ix_runtime_tasks_child_agent_id", table_name="runtime_tasks")
    op.drop_index("ix_runtime_tasks_parent_agent_id", table_name="runtime_tasks")
    op.drop_table("runtime_tasks")
    op.drop_column("agents", "execution_mode")
