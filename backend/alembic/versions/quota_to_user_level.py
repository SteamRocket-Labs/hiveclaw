"""Move quota enforcement from Agent level to User level.

Agent max_tokens/max_llm_calls fields removed — enforcement now on User.
Agent usage tracking fields (tokens_used_*) kept for statistics.

Revision ID: quota_to_user_level_0327
Revises: remove_agent_admin_role_0327
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "quota_to_user_level_0327"
down_revision: Union[str, Sequence[str], None] = "remove_agent_admin_role_0327"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── User: add token quota fields (idempotent) ──
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_tokens_per_day INTEGER")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_tokens_per_month INTEGER")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tokens_used_today INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tokens_used_month INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tokens_used_total INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tokens_reset_at TIMESTAMPTZ")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_llm_calls_per_day INTEGER NOT NULL DEFAULT 200")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS llm_calls_today INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS llm_calls_reset_at TIMESTAMPTZ")

    # ── Tenant: add default token quotas (idempotent) ──
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS default_tokens_per_day INTEGER")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS default_tokens_per_month INTEGER")

    # ── Migrate: copy Agent quota values to their creator User ──
    # For each user, take the MAX of their agents' limits as the user-level limit
    op.execute("""
        UPDATE users u SET
            quota_tokens_per_day = COALESCE(sub.max_daily, 100000),
            quota_llm_calls_per_day = COALESCE(sub.max_calls, 200)
        FROM (
            SELECT creator_id,
                   MAX(max_tokens_per_day) as max_daily,
                   MAX(max_llm_calls_per_day) as max_calls
            FROM agents
            GROUP BY creator_id
        ) sub
        WHERE u.id = sub.creator_id
    """)

    # ── Agent: remove quota enforcement fields (keep usage stats, idempotent) ──
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS max_tokens_per_day")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS max_tokens_per_month")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS max_llm_calls_per_day")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS llm_calls_today")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS llm_calls_reset_at")


def downgrade() -> None:
    # Restore Agent quota columns
    op.add_column("agents", sa.Column("max_tokens_per_day", sa.Integer(), nullable=True))
    op.add_column("agents", sa.Column("max_tokens_per_month", sa.Integer(), nullable=True))
    op.add_column("agents", sa.Column("max_llm_calls_per_day", sa.Integer(), nullable=False, server_default="100"))
    op.add_column("agents", sa.Column("llm_calls_today", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("agents", sa.Column("llm_calls_reset_at", sa.DateTime(timezone=True), nullable=True))

    # Drop Tenant columns
    op.drop_column("tenants", "default_tokens_per_month")
    op.drop_column("tenants", "default_tokens_per_day")

    # Drop User columns
    op.drop_column("users", "llm_calls_reset_at")
    op.drop_column("users", "llm_calls_today")
    op.drop_column("users", "quota_llm_calls_per_day")
    op.drop_column("users", "tokens_reset_at")
    op.drop_column("users", "tokens_used_total")
    op.drop_column("users", "tokens_used_month")
    op.drop_column("users", "tokens_used_today")
    op.drop_column("users", "quota_tokens_per_month")
    op.drop_column("users", "quota_tokens_per_day")
