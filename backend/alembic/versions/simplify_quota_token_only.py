"""Simplify quota to token-only — remove message/agent/TTL/LLM-call limits.

Revision ID: simplify_quota_token_only_0327
Revises: quota_to_user_level_0327
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "simplify_quota_token_only_0327"
down_revision: Union[str, Sequence[str], None] = "quota_to_user_level_0327"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── User: remove old quota fields ──
    op.drop_column("users", "quota_message_limit")
    op.drop_column("users", "quota_message_period")
    op.drop_column("users", "quota_messages_used")
    op.drop_column("users", "quota_period_start")
    op.drop_column("users", "quota_max_agents")
    op.drop_column("users", "quota_agent_ttl_hours")
    op.drop_column("users", "quota_llm_calls_per_day")
    op.drop_column("users", "llm_calls_today")
    op.drop_column("users", "llm_calls_reset_at")

    # ── Agent: remove expiry fields ──
    op.drop_column("agents", "expires_at")
    op.drop_column("agents", "is_expired")

    # ── Tenant: remove old default quota fields ──
    op.drop_column("tenants", "default_message_limit")
    op.drop_column("tenants", "default_message_period")
    op.drop_column("tenants", "default_max_agents")
    op.drop_column("tenants", "default_agent_ttl_hours")
    op.drop_column("tenants", "default_max_llm_calls_per_day")


def downgrade() -> None:
    # Tenant
    op.add_column("tenants", sa.Column("default_max_llm_calls_per_day", sa.Integer(), server_default="100"))
    op.add_column("tenants", sa.Column("default_agent_ttl_hours", sa.Integer(), server_default="48"))
    op.add_column("tenants", sa.Column("default_max_agents", sa.Integer(), server_default="2"))
    op.add_column("tenants", sa.Column("default_message_period", sa.String(20), server_default="permanent"))
    op.add_column("tenants", sa.Column("default_message_limit", sa.Integer(), server_default="50"))

    # Agent
    op.add_column("agents", sa.Column("is_expired", sa.Boolean(), server_default=sa.text("false")))
    op.add_column("agents", sa.Column("expires_at", sa.DateTime(timezone=True)))

    # User
    op.add_column("users", sa.Column("llm_calls_reset_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("llm_calls_today", sa.Integer(), server_default="0"))
    op.add_column("users", sa.Column("quota_llm_calls_per_day", sa.Integer(), server_default="200"))
    op.add_column("users", sa.Column("quota_agent_ttl_hours", sa.Integer(), server_default="48"))
    op.add_column("users", sa.Column("quota_max_agents", sa.Integer(), server_default="2"))
    op.add_column("users", sa.Column("quota_period_start", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("quota_messages_used", sa.Integer(), server_default="0"))
    op.add_column("users", sa.Column("quota_message_period", sa.String(20), server_default="permanent"))
    op.add_column("users", sa.Column("quota_message_limit", sa.Integer(), server_default="50"))
