"""Add online indexes for bounded dashboard and recovery queries.

Revision ID: query_resource_safety_0721
Revises: im_unverified_transport_0719
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa


revision = "query_resource_safety_0721"
down_revision = "im_unverified_transport_0719"
branch_labels = None
depends_on = None


_INDEXES = (
    (
        "ix_agent_activity_logs_agent_created_at",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_agent_activity_logs_agent_created_at "
        "ON agent_activity_logs (agent_id, created_at DESC)",
    ),
    (
        "ix_agent_activity_logs_agent_action_created_at",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_agent_activity_logs_agent_action_created_at "
        "ON agent_activity_logs (agent_id, action_type, created_at DESC)",
    ),
    (
        "ix_agent_activity_logs_tenant_created_at",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_agent_activity_logs_tenant_created_at "
        "ON agent_activity_logs (tenant_id, created_at DESC)",
    ),
    (
        "ix_agent_activity_logs_tenant_action_created_at",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_agent_activity_logs_tenant_action_created_at "
        "ON agent_activity_logs (tenant_id, action_type, created_at DESC)",
    ),
    (
        "ix_chat_sessions_dashboard_recent",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chat_sessions_dashboard_recent "
        "ON chat_sessions (tenant_id, user_id, listed_surface, last_message_at DESC NULLS LAST, created_at DESC)",
    ),
    (
        "ix_chat_messages_conversation_created_at",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chat_messages_conversation_created_at "
        "ON chat_messages (conversation_id, created_at)",
    ),
    (
        "ix_runtime_notification_outbox_source_lookup",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_notification_outbox_source_lookup "
        "ON runtime_notification_outbox (tenant_id, source_run_id)",
    ),
    (
        "ix_resource_permissions_principal_resource",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resource_permissions_principal_resource "
        "ON resource_permissions (tenant_id, principal_type, principal_id, resource_type, resource_id)",
    ),
    (
        "ix_runtime_tasks_notification_reconcile",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_tasks_notification_reconcile "
        "ON runtime_tasks (completed_at DESC NULLS LAST, created_at DESC) "
        "WHERE task_type IN ('subagent', 'team_member', 'workflow', 'delegation', 'a2a_delegation', "
        "'trigger', 'approval_execution') "
        "AND status IN ('completed', 'failed', 'killed', 'skipped', 'needs_reconciliation') "
        "AND tenant_id IS NOT NULL AND parent_agent_id IS NOT NULL",
    ),
)


def _drop_invalid_index(name: str) -> None:
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": name},
        )
        .scalar()
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, statement in _INDEXES:
            _drop_invalid_index(name)
            op.execute(statement)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _statement in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
