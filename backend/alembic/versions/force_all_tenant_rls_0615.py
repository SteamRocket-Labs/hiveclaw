"""Force RLS on all tenant-scoped runtime tables.

Revision ID: force_all_tenant_rls_0615
Revises: add_plugin_dependency_edges_0615
Create Date: 2026-06-15
"""

from __future__ import annotations

from typing import Union

from alembic import op
from sqlalchemy import inspect

revision: str = "force_all_tenant_rls_0615"
down_revision: Union[str, None] = "add_plugin_dependency_edges_0615"
branch_labels: Union[str, tuple[str, ...], None] = None
depends_on: Union[str, tuple[str, ...], None] = None

_FORCE_TABLES: tuple[str, ...] = (
    "agents",
    "users",
    "llm_models",
    "skills",
    "tools",
    "plaza_posts",
    "org_departments",
    "org_members",
    "config_revisions",
    "agent_mcp_server_assignments",
    "agent_mcp_tool_overrides",
    "agent_plan_recommendations",
    "agent_plan_requests",
    "agent_templates",
    "agent_work_ledgers",
    "capability_policies",
    "charter_proposals",
    "departments",
    "enterprise_info",
    "guard_policies",
    "identity_providers",
    "invitation_codes",
    "mcp_server_tools",
    "mcp_servers",
    "resource_permissions",
    "security_audit_events",
    "sso_scan_sessions",
    "tenant_channel_configs",
    "tenant_settings",
    "tenant_tool_configs",
    "token_usage_events",
    "agent_activity_logs",
    "agent_agent_relationships",
    "agent_capability_installs",
    "agent_permissions",
    "agent_relationships",
    "agent_schedules",
    "agent_tools",
    "agent_triggers",
    "approval_requests",
    "audit_logs",
    "channel_configs",
    "chat_messages",
    "chat_sessions",
    "gateway_messages",
    "pending_reply_contexts",
    "runtime_tasks",
    "task_logs",
    "tasks",
    "workflow_definitions",
    "workflow_steps",
    "workflow_leaf_calls",
    "workflow_quotas",
    "coordination_leases",
    "coordination_signals",
    "coordination_checkpoints",
    "session_feedback_events",
    "invocation_spans",
    "tenant_installed_plugins",
    "agent_plugin_assignments",
    "plugin_hook_registrations",
    "plugin_dependency_edges",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    for table in _FORCE_TABLES:
        if table not in existing:
            continue
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    existing = _existing_tables()
    for table in reversed(_FORCE_TABLES):
        if table not in existing:
            continue
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
