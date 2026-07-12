"""Make tenant NULL semantics explicit and fail closed.

Revision ID: tenant_null_semantics_0712
Revises: rls_complete_coverage_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "tenant_null_semantics_0712"
down_revision = "rls_complete_coverage_0712"
branch_labels = None
depends_on = None


QUARANTINE_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000000023")
QUARANTINE_TENANT_SLUG = "__hive_scope_quarantine__"

# These tables legitimately expose a narrowly defined platform row to tenant
# readers. Tenant sessions may never INSERT/UPDATE that global row: USING and
# WITH CHECK are intentionally different.
PLATFORM_SHARED: dict[str, str] = {
    "agent_templates": "agent_templates.tenant_id IS NULL AND agent_templates.is_builtin IS TRUE",
    "identity_providers": "identity_providers.tenant_id IS NULL",
    "llm_models": "llm_models.tenant_id IS NULL",
    "runtime_budget_policies": (
        "runtime_budget_policies.tenant_id IS NULL AND runtime_budget_policies.scope_type = 'platform_default'"
    ),
    "skills": "skills.tenant_id IS NULL AND skills.is_builtin IS TRUE",
    "tools": "tools.tenant_id IS NULL AND tools.type = 'builtin'",
    "workflow_definitions": (
        "workflow_definitions.tenant_id IS NULL "
        "AND workflow_definitions.visibility_scope = 'platform' "
        "AND workflow_definitions.owner_type = 'platform'"
    ),
}

OPERATOR_NULLABLE = {"audit_logs", "users"}

TENANT_OWNED_TABLES: tuple[str, ...] = (
    "agent_activity_logs",
    "agent_agent_relationships",
    "agent_capability_installs",
    "agent_collaboration_group_members",
    "agent_collaboration_groups",
    "agent_mcp_server_assignments",
    "agent_mcp_tool_overrides",
    "agent_permissions",
    "agent_plan_recommendations",
    "agent_plan_requests",
    "agent_plugin_assignments",
    "agent_relationships",
    "agent_schedules",
    "agent_session_goals",
    "agent_teams",
    "agent_tools",
    "agent_triggers",
    "agent_work_ledgers",
    "agents",
    "ai_asset_records",
    "ai_asset_usage_events",
    "approval_requests",
    "budget_transition_outbox",
    "capability_factor_reviews",
    "capability_factors",
    "capability_policies",
    "capability_promotion_proposals",
    "channel_configs",
    "channel_delivery_outbox",
    "channel_ingress_events",
    "charter_proposals",
    "chat_artifacts",
    "chat_messages",
    "chat_sessions",
    "chat_transcript_events",
    "config_revisions",
    "coordination_checkpoints",
    "coordination_leases",
    "coordination_signals",
    "decision_trace_feedback",
    "decision_traces",
    "departments",
    "enterprise_info",
    "external_capability_reviews",
    "external_capability_snapshots",
    "external_extension_activations",
    "external_extension_catalog_entries",
    "external_extension_components",
    "external_extension_hook_registrations",
    "external_marketplace_entries",
    "external_marketplace_sources",
    "external_principal_binding_events",
    "external_principals",
    "guard_policies",
    "hr_creation_drafts",
    "hr_provisioning_steps",
    "invitation_codes",
    "invocation_spans",
    "knowledge_assertions",
    "knowledge_documents",
    "knowledge_entities",
    "knowledge_grants",
    "knowledge_index_jobs",
    "knowledge_links",
    "knowledge_segments",
    "local_agent_bridge_connections",
    "local_agent_bridge_pairing_sessions",
    "local_agent_capability_snapshots",
    "local_agent_channel_events",
    "local_agent_channel_messages",
    "local_agent_channel_sessions",
    "local_agent_channel_ws_tickets",
    "local_agent_channels",
    "mcp_server_tools",
    "mcp_servers",
    "org_departments",
    "org_members",
    "pending_reply_contexts",
    "personal_knowledge_proposals",
    "plaza_posts",
    "plugin_dependency_edges",
    "plugin_hook_registrations",
    "resource_permissions",
    "runtime_budget_events",
    "runtime_budget_runs",
    "runtime_notification_outbox",
    "runtime_tasks",
    "security_audit_events",
    "session_feedback_events",
    "sso_scan_sessions",
    "task_logs",
    "tasks",
    "tenant_channel_configs",
    "tenant_installed_plugins",
    "tenant_scope_quarantine_records",
    "tenant_settings",
    "tenant_tool_configs",
    "token_usage_events",
    "workflow_leaf_calls",
    "workflow_preview_artifacts",
    "workflow_promotion_proposals",
    "workflow_proposal_artifacts",
    "workflow_quotas",
    "workflow_steps",
    "workspace_resource_manifests",
)

# Explicit authoritative joins for the 44 legacy nullable tenant-owned tables.
# Each tuple is (source table, local column, source key). Multiple distinct
# candidate tenants are treated as corruption and quarantined, never guessed.
BACKFILL_SOURCES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "agent_activity_logs": (("agents", "agent_id", "id"), ("chat_sessions", "root_session_id", "id")),
    "agent_agent_relationships": (("agents", "agent_id", "id"), ("agents", "target_agent_id", "id")),
    "agent_capability_installs": (("agents", "agent_id", "id"),),
    "agent_permissions": (("agents", "agent_id", "id"),),
    "agent_plan_recommendations": (("agents", "agent_id", "id"),),
    "agent_plan_requests": (("agents", "agent_id", "id"),),
    "agent_relationships": (("agents", "agent_id", "id"), ("org_members", "member_id", "id")),
    "agent_schedules": (("agents", "agent_id", "id"), ("users", "created_by", "id")),
    "agent_session_goals": (("chat_sessions", "chat_session_id", "id"), ("agents", "agent_id", "id")),
    "agent_teams": (("agents", "lead_agent_id", "id"), ("chat_sessions", "parent_session_id", "id")),
    "agent_tools": (("agents", "agent_id", "id"),),
    "agent_triggers": (("agents", "agent_id", "id"),),
    "agent_work_ledgers": (("agents", "agent_id", "id"), ("users", "user_id", "id")),
    "agents": (
        ("users", "owner_user_id", "id"),
        ("users", "sponsor_user_id", "id"),
        ("users", "creator_id", "id"),
    ),
    "approval_requests": (("agents", "agent_id", "id"), ("runtime_tasks", "execution_task_id", "id")),
    "channel_configs": (("agents", "agent_id", "id"),),
    "chat_artifacts": (
        ("chat_sessions", "session_id", "id"),
        ("agents", "agent_id", "id"),
        ("runtime_tasks", "runtime_task_id", "id"),
    ),
    "chat_messages": (("agents", "agent_id", "id"), ("users", "user_id", "id")),
    "chat_sessions": (("agents", "agent_id", "id"), ("users", "user_id", "id")),
    "chat_transcript_events": (
        ("chat_sessions", "session_id", "id"),
        ("agents", "agent_id", "id"),
        ("runtime_tasks", "run_id", "id"),
    ),
    "decision_trace_feedback": (("decision_traces", "decision_id", "decision_id"),),
    "decision_traces": (("agents", "agent_id", "id"), ("users", "user_id", "id")),
    "departments": (("users", "manager_id", "id"), ("departments", "parent_id", "id")),
    "enterprise_info": (("users", "updated_by", "id"),),
    "invitation_codes": (("users", "created_by", "id"),),
    "local_agent_bridge_pairing_sessions": (
        ("local_agent_bridge_connections", "connection_id", "id"),
        ("agents", "agent_id", "id"),
        ("users", "user_id", "id"),
    ),
    "local_agent_channel_events": (
        ("local_agent_channel_sessions", "session_id", "id"),
        ("local_agent_channel_messages", "message_id", "id"),
        ("agents", "source_agent_id", "id"),
    ),
    "local_agent_channel_messages": (
        ("local_agent_channel_sessions", "session_id", "id"),
        ("agents", "source_agent_id", "id"),
        ("agents", "sender_agent_id", "id"),
        ("users", "owner_user_id", "id"),
    ),
    "local_agent_channel_sessions": (
        ("local_agent_channels", "channel_id", "id"),
        ("local_agent_bridge_connections", "connection_id", "id"),
        ("agents", "source_agent_id", "id"),
        ("users", "owner_user_id", "id"),
    ),
    "org_departments": (("org_departments", "parent_id", "id"),),
    "org_members": (("org_departments", "department_id", "id"),),
    "pending_reply_contexts": (("agents", "agent_id", "id"),),
    "plaza_posts": (("agents", "author_id", "id"), ("users", "author_id", "id")),
    "runtime_budget_events": (("runtime_budget_runs", "budget_run_id", "id"),),
    "runtime_budget_runs": (
        ("runtime_budget_policies", "policy_id", "id"),
        ("runtime_tasks", "root_runtime_task_id", "id"),
        ("agents", "root_agent_id", "id"),
        ("users", "root_user_id", "id"),
        ("external_principals", "root_external_principal_id", "id"),
    ),
    "runtime_tasks": (
        ("runtime_budget_runs", "budget_run_id", "id"),
        ("agents", "parent_agent_id", "id"),
        ("agents", "child_agent_id", "id"),
        ("users", "root_user_id", "id"),
    ),
    "session_feedback_events": (
        ("chat_sessions", "session_id", "id"),
        ("agents", "agent_id", "id"),
        ("users", "user_id", "id"),
    ),
    "sso_scan_sessions": (("users", "user_id", "id"),),
    "task_logs": (("tasks", "task_id", "id"),),
    "tasks": (
        ("agents", "agent_id", "id"),
        ("chat_sessions", "root_session_id", "id"),
        ("users", "created_by", "id"),
        ("runtime_tasks", "active_runtime_task_id", "id"),
    ),
    "token_usage_events": (("agents", "agent_id", "id"), ("users", "user_id", "id")),
    "workflow_leaf_calls": (("runtime_tasks", "run_id", "id"),),
    "workflow_quotas": (("runtime_tasks", "run_id", "id"),),
    "workflow_steps": (("runtime_tasks", "run_id", "id"),),
}


def _bypass() -> str:
    return "current_setting('app.current_tenant_id', true) = 'BYPASS'"


def _strict(table: str) -> str:
    return f"{_bypass()} OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)"


def _quote(identifier: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _candidate_union(table: str) -> str | None:
    sources = BACKFILL_SOURCES.get(table, ())
    if not sources:
        return None
    parts = []
    for source_table, local_column, source_key in sources:
        parts.append(
            "SELECT source.tenant_id "
            f"FROM {_quote(source_table)} AS source "
            f"WHERE source.{_quote(source_key)} = target.{_quote(local_column)} "
            "AND source.tenant_id IS NOT NULL"
        )
    return " UNION ALL ".join(parts)


def _backfill_unique_authority(table: str) -> int:
    union = _candidate_union(table)
    if union is None:
        return 0
    quoted = _quote(table)
    result = op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {quoted} AS target
               SET tenant_id = candidate.tenant_id
              FROM (
                    SELECT target.id,
                           min(authority.tenant_id::text)::uuid AS tenant_id
                      FROM {quoted} AS target
                      CROSS JOIN LATERAL ({union}) AS authority
                     WHERE target.tenant_id IS NULL
                     GROUP BY target.id
                    HAVING count(DISTINCT authority.tenant_id) = 1
                   ) AS candidate
             WHERE target.id = candidate.id
               AND target.tenant_id IS NULL
            """
        )
    )
    return max(int(result.rowcount or 0), 0)


def _install_policy(table: str, using: str, check: str) -> None:
    policy = f"tenant_isolation_{table}"
    op.execute(f"ALTER TABLE {_quote(table)} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_quote(table)} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {_quote(policy)} ON {_quote(table)}")
    op.execute(f"CREATE POLICY {_quote(policy)} ON {_quote(table)} USING ({using}) WITH CHECK ({check})")


def _derived_policy(table: str) -> tuple[str, str] | None:
    if table in {"notifications", "refresh_tokens"}:
        predicate = (
            f"{_bypass()} OR EXISTS (SELECT 1 FROM users "
            f"WHERE users.id = {table}.user_id AND "
            "users.tenant_id::text = current_setting('app.current_tenant_id', true))"
        )
        return predicate, predicate
    if table in {"plaza_comments", "plaza_likes"}:
        predicate = (
            f"{_bypass()} OR EXISTS (SELECT 1 FROM plaza_posts "
            f"WHERE plaza_posts.id = {table}.post_id AND "
            "plaza_posts.tenant_id::text = current_setting('app.current_tenant_id', true))"
        )
        return predicate, predicate
    if table == "skill_files":
        using = (
            f"{_bypass()} OR EXISTS (SELECT 1 FROM skills WHERE skills.id = skill_files.skill_id AND "
            "(skills.tenant_id::text = current_setting('app.current_tenant_id', true) OR "
            "(skills.tenant_id IS NULL AND skills.is_builtin IS TRUE)))"
        )
        check = (
            f"{_bypass()} OR EXISTS (SELECT 1 FROM skills WHERE skills.id = skill_files.skill_id AND "
            "skills.tenant_id::text = current_setting('app.current_tenant_id', true))"
        )
        return using, check
    if table == "external_identities":
        predicate = (
            f"{_bypass()} OR EXISTS (SELECT 1 FROM users WHERE users.id = external_identities.user_id AND "
            "users.tenant_id::text = current_setting('app.current_tenant_id', true))"
        )
        return predicate, predicate
    if table == "participants":
        predicate = (
            f"{_bypass()} OR (participants.type = 'user' AND EXISTS (SELECT 1 FROM users "
            "WHERE users.id = participants.ref_id AND "
            "users.tenant_id::text = current_setting('app.current_tenant_id', true))) OR "
            "(participants.type = 'agent' AND EXISTS (SELECT 1 FROM agents "
            "WHERE agents.id = participants.ref_id AND "
            "agents.tenant_id::text = current_setting('app.current_tenant_id', true)))"
        )
        return predicate, predicate
    if table in {"agent_team_members", "agent_team_events"}:
        predicate = (
            f"{_bypass()} OR EXISTS (SELECT 1 FROM agent_teams "
            f"WHERE agent_teams.id = {table}.team_id AND "
            "agent_teams.tenant_id::text = current_setting('app.current_tenant_id', true))"
        )
        return predicate, predicate
    return None


def _create_quarantine_table(existing: set[str]) -> None:
    if "tenant_scope_quarantine_records" in existing:
        return
    op.create_table(
        "tenant_scope_quarantine_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_table", sa.String(length=100), nullable=False),
        sa.Column("source_row_id", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_table", "source_row_id", name="uq_tenant_scope_quarantine_source"),
    )
    op.create_index(
        "ix_tenant_scope_quarantine_records_tenant_id",
        "tenant_scope_quarantine_records",
        ["tenant_id"],
    )


def _quarantine_residuals(existing: set[str]) -> None:
    bind = op.get_bind()
    residual_tables = []
    for table in BACKFILL_SOURCES:
        if table not in existing:
            continue
        count = bind.scalar(sa.text(f"SELECT count(*) FROM {_quote(table)} WHERE tenant_id IS NULL")) or 0
        if int(count):
            residual_tables.append(table)
    if not residual_tables:
        return

    bind.execute(
        sa.text(
            """
            INSERT INTO tenants (
                id, name, slug, im_provider, is_active,
                min_heartbeat_interval_minutes, timezone,
                default_max_triggers, min_poll_interval_floor, max_webhook_rate_ceiling,
                tokens_used_today, tokens_used_month, tokens_used_total, sync_version
            )
            VALUES (
                :id, 'Tenant Scope Quarantine', :slug, 'web_only', false,
                45, 'UTC', 20, 5, 5, 0, 0, 0, 1
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"id": QUARANTINE_TENANT_ID, "slug": QUARANTINE_TENANT_SLUG},
    )
    for table in residual_tables:
        union = _candidate_union(table)
        authority_count = f"(SELECT count(DISTINCT authority.tenant_id) FROM ({union}) AS authority)" if union else "0"
        bind.execute(
            sa.text(
                f"""
                INSERT INTO tenant_scope_quarantine_records
                    (id, tenant_id, source_table, source_row_id, reason)
                SELECT md5(:table || ':' || target.id::text)::uuid,
                       :tenant_id,
                       :table,
                       target.id::text,
                       CASE WHEN {authority_count} > 1
                            THEN 'conflicting tenant authorities'
                            ELSE 'tenant authority could not be derived'
                       END
                  FROM {_quote(table)} AS target
                 WHERE target.tenant_id IS NULL
                ON CONFLICT (source_table, source_row_id) DO NOTHING
                """
            ),
            {"table": table, "tenant_id": QUARANTINE_TENANT_ID},
        )
    for table in residual_tables:
        bind.execute(
            sa.text(f"UPDATE {_quote(table)} SET tenant_id = :tenant_id WHERE tenant_id IS NULL"),
            {"tenant_id": QUARANTINE_TENANT_ID},
        )


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    _create_quarantine_table(existing)
    existing.add("tenant_scope_quarantine_records")

    # Fixed-point propagation handles child-before-parent legacy rows while
    # preserving conflict detection. No arbitrary source precedence is used.
    for _ in range(len(BACKFILL_SOURCES)):
        updated = sum(_backfill_unique_authority(table) for table in BACKFILL_SOURCES if table in existing)
        if updated == 0:
            break
    _quarantine_residuals(existing)

    for table in TENANT_OWNED_TABLES:
        if table in existing:
            op.execute(f"ALTER TABLE {_quote(table)} ALTER COLUMN tenant_id SET NOT NULL")

    for table in TENANT_OWNED_TABLES:
        if table in existing:
            predicate = _strict(table)
            _install_policy(table, predicate, predicate)
    for table, shared_predicate in PLATFORM_SHARED.items():
        if table in existing:
            _install_policy(table, f"{_strict(table)} OR ({shared_predicate})", _strict(table))
    for table in OPERATOR_NULLABLE:
        if table in existing:
            predicate = _strict(table)
            _install_policy(table, predicate, predicate)
    for table in (
        "notifications",
        "refresh_tokens",
        "plaza_comments",
        "plaza_likes",
        "skill_files",
        "external_identities",
        "participants",
        "agent_team_members",
        "agent_team_events",
    ):
        if table in existing:
            predicates = _derived_policy(table)
            if predicates:
                _install_policy(table, *predicates)


def downgrade() -> None:
    # Secure downgrade: restoring nullable globally visible rows or the old
    # shared WITH CHECK policy would reintroduce cross-tenant exposure. Older
    # application code accepts non-null tenant-owned rows and stricter RLS, so
    # preserve constraints, quarantine receipts, and policies. Re-upgrade is
    # idempotent.
    pass
