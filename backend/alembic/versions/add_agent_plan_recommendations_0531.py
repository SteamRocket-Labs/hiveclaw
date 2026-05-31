"""Add agent_plan_recommendations — Plan Mode recommendation ledger.

Revision ID: add_agent_plan_recommendations_0531
Revises: add_agent_plan_requests_0529
Create Date: 2026-05-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "add_agent_plan_recommendations_0531"
down_revision: Union[str, None] = "add_agent_plan_requests_0529"
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
    if not _table_exists("agent_plan_recommendations"):
        op.create_table(
            "agent_plan_recommendations",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column(
                "agent_id",
                UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("session_id", sa.String(length=100), nullable=False),
            sa.Column("runtime_task_id", UUID(as_uuid=True), nullable=True),
            sa.Column(
                "recommended_to_user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("source", sa.String(length=30), nullable=False, server_default="web_chat"),
            sa.Column("intent_type", sa.String(length=30), nullable=False, server_default="autonomous_wake"),
            sa.Column("action_kind", sa.String(length=50), nullable=False, server_default="create_enabled_trigger"),
            sa.Column("tool_name", sa.String(length=80), nullable=False, server_default="set_trigger"),
            sa.Column("title", sa.Text(), nullable=False, server_default=""),
            sa.Column("original_request", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="recommended"),
            sa.Column("declined_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("accepted_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("metadata_json", JSONB(), nullable=True),
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_recommendations_tenant_id "
        "ON agent_plan_recommendations (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_recommendations_agent_id "
        "ON agent_plan_recommendations (agent_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_recommendations_session_id "
        "ON agent_plan_recommendations (session_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_recommendations_runtime_task_id "
        "ON agent_plan_recommendations (runtime_task_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_recommendations_recommended_to_user_id "
        "ON agent_plan_recommendations (recommended_to_user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_recommendations_agent_status "
        "ON agent_plan_recommendations (agent_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_recommendations_session_status "
        "ON agent_plan_recommendations (session_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_plan_recommendations_user_session "
        "ON agent_plan_recommendations (recommended_to_user_id, session_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_recommendations_user_session")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_recommendations_session_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_recommendations_agent_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_recommendations_recommended_to_user_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_recommendations_runtime_task_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_recommendations_session_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_recommendations_agent_id")
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_recommendations_tenant_id")
    op.drop_table("agent_plan_recommendations")
