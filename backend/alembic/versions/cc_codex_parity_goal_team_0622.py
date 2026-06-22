"""Add CC/Codex parity goal and team control indexes.

Revision ID: cc_codex_parity_goal_team_0622
Revises: local_agent_bridge_0622
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "cc_codex_parity_goal_team_0622"
down_revision = "local_agent_bridge_0622"
branch_labels = None
depends_on = None


def _jsonb():
    return postgresql.JSONB(astext_type=sa.Text())


def _table_exists(table_name: str) -> bool:
    try:
        bind = op.get_bind()
    except AttributeError:
        return False
    return sa.inspect(bind).has_table(table_name)


def upgrade() -> None:
    if not _table_exists("agent_session_goals"):
        op.create_table(
            "agent_session_goals",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
            sa.Column(
                "chat_session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id"), nullable=False
            ),
            sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("token_budget", sa.Integer(), nullable=True),
            sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("time_budget_seconds", sa.Integer(), nullable=True),
            sa.Column("continuation_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_continuation_turns", sa.Integer(), nullable=True),
            sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_summary", sa.Text(), nullable=True),
            sa.Column("metadata_json", _jsonb(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _table_exists("agent_teams"):
        op.create_table(
            "agent_teams",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("lead_agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
            sa.Column(
                "parent_session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id"), nullable=False
            ),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("transcript_truth", sa.String(length=64), nullable=False, server_default="chat_session_t0"),
            sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("metadata_json", _jsonb(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _table_exists("agent_team_members"):
        op.create_table(
            "agent_team_members",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("team_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_teams.id"), nullable=False),
            sa.Column("member_name", sa.String(length=160), nullable=False),
            sa.Column("member_role", sa.Text(), nullable=True),
            sa.Column("model_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("llm_models.id"), nullable=True),
            sa.Column(
                "chat_session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id"), nullable=False
            ),
            sa.Column(
                "runtime_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runtime_tasks.id"), nullable=True
            ),
            sa.Column("runtime_task_type", sa.String(length=64), nullable=False, server_default="team_member"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="idle"),
            sa.Column("tool_policy_json", _jsonb(), nullable=True),
            sa.Column("budget_json", _jsonb(), nullable=True),
            sa.Column("metadata_json", _jsonb(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _table_exists("agent_team_events"):
        op.create_table(
            "agent_team_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("team_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_teams.id"), nullable=False),
            sa.Column(
                "sender_member_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agent_team_members.id"),
                nullable=True,
            ),
            sa.Column(
                "receiver_member_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agent_team_members.id"),
                nullable=True,
            ),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("payload_json", _jsonb(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_session_goals_active_session ON agent_session_goals(agent_id, chat_session_id) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_session_goals_tenant_status ON agent_session_goals(tenant_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_teams_lead_session ON agent_teams(lead_agent_id, parent_session_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_teams_tenant_status ON agent_teams(tenant_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_team_members_team_status ON agent_team_members(team_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_team_members_session ON agent_team_members(chat_session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_team_events_team_created ON agent_team_events(team_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_team_events_type ON agent_team_events(event_type)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_team_events_type")
    op.execute("DROP INDEX IF EXISTS ix_agent_team_events_team_created")
    op.execute("DROP INDEX IF EXISTS ix_agent_team_members_session")
    op.execute("DROP INDEX IF EXISTS ix_agent_team_members_team_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_teams_tenant_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_teams_lead_session")
    op.execute("DROP INDEX IF EXISTS ix_agent_session_goals_tenant_status")
    op.execute("DROP INDEX IF EXISTS ix_agent_session_goals_active_session")
    op.drop_table("agent_team_events")
    op.drop_table("agent_team_members")
    op.drop_table("agent_teams")
    op.drop_table("agent_session_goals")
