"""Add agent_plan_requests — canonical Plan Mode ledger.

Revision ID: add_agent_plan_requests_0529
Revises: raise_agent_tool_round_defaults_0526
Create Date: 2026-05-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "add_agent_plan_requests_0529"
down_revision: Union[str, None] = "raise_agent_tool_round_defaults_0526"
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
    if not _table_exists("agent_plan_requests"):
        op.create_table(
            "agent_plan_requests",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column(
                "agent_id",
                UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("session_id", sa.String(length=100), nullable=True),
            sa.Column("runtime_task_id", UUID(as_uuid=True), nullable=True),
            sa.Column(
                "requested_by_user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column("source", sa.String(length=30), nullable=False, server_default="web_chat"),
            sa.Column("intent_type", sa.String(length=30), nullable=False),
            sa.Column("original_request", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
            sa.Column("plan_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("plan_hash", sa.String(length=80), nullable=True),
            sa.Column("plan_markdown_path", sa.Text(), nullable=True),
            sa.Column("plan_json", JSONB(), nullable=False, server_default="{}"),
            sa.Column("handoff_payload", JSONB(), nullable=True),
            sa.Column("handoff_status", sa.String(length=20), nullable=True),
            sa.Column("confirmed_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rejected_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("superseded_by_plan_id", UUID(as_uuid=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("metadata_json", JSONB(), nullable=True),
        )

    # Single-column indexes mirroring the model's index=True columns.
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_tenant_id ON agent_plan_requests (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_agent_id ON agent_plan_requests (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_session_id ON agent_plan_requests (session_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_runtime_task_id ON agent_plan_requests (runtime_task_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_requested_by_user_id "
        "ON agent_plan_requests (requested_by_user_id)"
    )

    # Composite indexes from __table_args__ (§6.1).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_agent_status ON agent_plan_requests (agent_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_tenant_status ON agent_plan_requests (tenant_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_session_created "
        "ON agent_plan_requests (session_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_requests_runtime_task ON agent_plan_requests (runtime_task_id)"
    )
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_id UUID")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_version INTEGER")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_hash VARCHAR(80)")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_exempt_reason VARCHAR(100)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_runtime_task")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_session_created")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_tenant_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_agent_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_requested_by_user_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_runtime_task_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_session_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_agent_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_requests_tenant_id")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS plan_exempt_reason")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS plan_hash")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS plan_version")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS plan_id")
    op.drop_table("agent_plan_requests")
