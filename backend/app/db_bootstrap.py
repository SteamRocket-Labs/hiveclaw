"""Helpers for bootstrapping an unversioned database to the current schema."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.schema import MetaData

_ALEMBIC_VERSION_TABLE = "alembic_version"
_ALEMBIC_VERSION_NUM_LENGTH = 255
_CORE_APP_TABLES = {
    "users",
    "agents",
    "tenants",
    "chat_artifacts",
    "chat_messages",
    "chat_sessions",
    "chat_transcript_events",
    "tasks",
    "llm_models",
}

# Tenant tables that must carry RLS from day one. Mirrors
# alembic/versions/add_row_level_security.py (_TENANT_TABLES) — that historic
# migration is immutable, so the bootstrap path keeps its own copy; keep the
# two lists in sync when adding tenant tables.
RLS_TENANT_TABLES: tuple[str, ...] = (
    "agents",
    "users",
    "llm_models",
    "skills",
    "tools",
    "plaza_posts",
    "org_departments",
    "org_members",
    "config_revisions",
    # Stage-2a (RLS enforcement migration, alembic rls_stage2a_tenant_policies_0610):
    # tenant-scoped tables that carried tenant_id but had NO policy — they would
    # leak cross-tenant under the non-owner role flip. Policy-only (column exists).
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
    # Stage-2b (RLS enforcement migration, alembic rls_stage2b_agent_scoped_0610):
    # agent-scoped tables that carried agent_id but NO tenant_id column — they
    # would leak cross-tenant under the non-owner role flip. The migration adds a
    # nullable tenant_id column + index; this list applies the policy on the
    # bootstrap (create_all) path. Keep in sync with _STAGE2B_TABLES.
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
    "chat_artifacts",
    "chat_messages",
    "chat_sessions",
    "chat_transcript_events",
    "local_agent_bridge_connections",
    "local_agent_bridge_pairing_sessions",
    "pending_reply_contexts",
    "runtime_tasks",
    "task_logs",
    "tasks",
)

# Additional table families that also need FORCE ROW LEVEL SECURITY. The
# production connection is the table owner, so ENABLE alone is inert for app
# reads; all RLS_TENANT_TABLES are included in RLS_FORCED_TENANT_TABLES below.
_ADDITIONAL_FORCED_TENANT_TABLES: tuple[str, ...] = (
    "workflow_definitions",
    "workflow_steps",
    "workflow_leaf_calls",
    "workflow_quotas",
    "runtime_budget_policies",
    "runtime_budget_runs",
    "runtime_budget_events",
    "coordination_leases",
    "coordination_signals",
    "coordination_checkpoints",
    "session_feedback_events",
    "invocation_spans",
    "decision_traces",
    "decision_trace_feedback",
    # Plugin system (Step 5): new tenant-scoped table family — starts in FORCED so
    # the owner production connection is also policy-bound (P0 gap B). Mirrors the
    # MCPServer install primitive (RLS ENABLE+FORCE, tenant_id mandatory).
    "tenant_installed_plugins",
    "agent_plugin_assignments",
    "plugin_hook_registrations",
    "plugin_dependency_edges",
    "external_capability_reviews",
    "external_capability_snapshots",
    "external_extension_catalog_entries",
    "external_extension_components",
    "external_extension_hook_registrations",
    "external_extension_activations",
    "external_marketplace_sources",
    "external_marketplace_entries",
    "capability_factors",
    "capability_factor_reviews",
    "capability_promotion_proposals",
    # Personal Knowledge core records. Scope columns remain schema-compatible
    # with future organization knowledge, but no Company KB runtime exists yet.
    # Keep RLS forced so owner-role connections cannot bypass Personal KB ACLs.
    "knowledge_documents",
    "knowledge_segments",
    "knowledge_entities",
    "knowledge_assertions",
    "knowledge_links",
    "knowledge_index_jobs",
    "knowledge_grants",
    "personal_knowledge_proposals",
    "local_agent_capability_snapshots",
    "ai_asset_records",
    "hr_creation_drafts",
    "hr_provisioning_steps",
    "workflow_proposal_artifacts",
    "workflow_preview_artifacts",
    "runtime_notification_outbox",
    "runtime_result_objects",
    "runtime_result_mailbox_cursors",
    "runtime_result_integration_pages",
    "channel_ingress_events",
    "external_principals",
    "external_principal_binding_events",
    "channel_delivery_outbox",
    "workflow_promotion_proposals",
    "budget_transition_outbox",
    # R-022 complete-coverage guard: these late table families already had
    # upgrade migrations, but were absent from create_all bootstrap. Fresh
    # databases therefore skipped their policies when Alembic stamped head.
    "agent_teams",
    "agent_team_members",
    "agent_team_events",
    "agent_collaboration_groups",
    "agent_collaboration_group_members",
    "agent_session_goals",
    "ai_asset_usage_events",
    "local_agent_channels",
    "local_agent_channel_events",
    "local_agent_channel_messages",
    "local_agent_channel_sessions",
    "local_agent_channel_ws_tickets",
    "workspace_resource_manifests",
    "tenant_scope_quarantine_records",
    "storage_blobs",
    "storage_blob_refs",
    "storage_gc_runs",
    # Session V2 canonical command/event/recovery plane.
    "session_event_cursors",
    "session_event_outbox",
    "session_commands",
    "session_turn_inputs",
    "session_input_admissions",
    "session_carry_forwards",
    "session_control_inputs",
    "session_turn_replacements",
    "session_tool_invocations",
    "session_model_results",
    "session_round_obligations",
    "session_next_round_plans",
    "session_run_outcomes",
    "session_feedback_aggregates",
    # Group 3 mixed direct/Subagent/Team/Workflow/A2A admission coverage.
    "runtime_root_items",
)

# Every table carrying ``tenant_id`` belongs to exactly one semantic class.
# Tenant-owned rows are always non-null and strict. Platform-shared rows use a
# table-specific read predicate and a strict write predicate. The two
# operator-nullable tables retain NULL only for pre-tenant/system records and
# ordinary tenant sessions never see those rows.
STRICT_TENANT_RLS_TABLES: tuple[str, ...] = (
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
    "external_extension_catalog_entries",
    "external_extension_components",
    "external_extension_hook_registrations",
    "external_extension_activations",
    "external_marketplace_sources",
    "external_marketplace_entries",
    "external_principal_binding_events",
    "external_principals",
    "guard_policies",
    "knowledge_documents",
    "knowledge_segments",
    "knowledge_entities",
    "knowledge_assertions",
    "knowledge_links",
    "knowledge_index_jobs",
    "knowledge_grants",
    "personal_knowledge_proposals",
    "local_agent_capability_snapshots",
    "hr_creation_drafts",
    "hr_provisioning_steps",
    "workflow_proposal_artifacts",
    "workflow_preview_artifacts",
    "runtime_notification_outbox",
    "runtime_result_objects",
    "runtime_result_mailbox_cursors",
    "runtime_result_integration_pages",
    "workflow_promotion_proposals",
    "invitation_codes",
    "invocation_spans",
    "local_agent_channels",
    "local_agent_channel_ws_tickets",
    "workspace_resource_manifests",
    "local_agent_bridge_connections",
    "local_agent_bridge_pairing_sessions",
    "local_agent_channel_events",
    "local_agent_channel_messages",
    "local_agent_channel_sessions",
    "mcp_server_tools",
    "mcp_servers",
    "org_departments",
    "org_members",
    "pending_reply_contexts",
    "plaza_posts",
    "plugin_dependency_edges",
    "plugin_hook_registrations",
    "resource_permissions",
    "runtime_budget_events",
    "runtime_budget_runs",
    "runtime_tasks",
    "runtime_root_items",
    "security_audit_events",
    "session_feedback_events",
    "session_event_cursors",
    "session_event_outbox",
    "session_commands",
    "session_turn_inputs",
    "session_input_admissions",
    "session_carry_forwards",
    "session_control_inputs",
    "session_turn_replacements",
    "session_tool_invocations",
    "session_model_results",
    "session_round_obligations",
    "session_next_round_plans",
    "session_run_outcomes",
    "session_feedback_aggregates",
    "sso_scan_sessions",
    "storage_blobs",
    "storage_blob_refs",
    "storage_gc_runs",
    "task_logs",
    "tasks",
    "tenant_channel_configs",
    "tenant_installed_plugins",
    "tenant_scope_quarantine_records",
    "tenant_settings",
    "tenant_tool_configs",
    "token_usage_events",
    "workflow_leaf_calls",
    "workflow_quotas",
    "workflow_steps",
)

OPERATOR_NULLABLE_TENANT_TABLES: tuple[str, ...] = ("audit_logs", "users")

PLATFORM_SHARED_TENANT_PREDICATES: dict[str, str] = {
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

REMAINING_GLOBAL_AND_DERIVED_RLS_TABLES: tuple[str, ...] = (
    "tenants",
    "notifications",
    "refresh_tokens",
    "plaza_comments",
    "plaza_likes",
    "skill_files",
    "external_identities",
    "participants",
    "feature_flags",
    "system_settings",
    "identities",
)

_SECRET_SYSTEM_SETTING_KEYS: tuple[str, ...] = (
    "jina_api_key",
    "tavily_api_key",
    "exa_api_key",
    "firecrawl_api_key",
)

RLS_FORCED_TENANT_TABLES: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *RLS_TENANT_TABLES,
            *_ADDITIONAL_FORCED_TENANT_TABLES,
            *REMAINING_GLOBAL_AND_DERIVED_RLS_TABLES,
        )
    )
)


def _bypass_predicate() -> str:
    return "current_setting('app.current_tenant_id', true) = 'BYPASS'"


def _standard_tenant_predicate(table: str) -> str:
    """Return the fail-closed tenant predicate retained for migration parity."""

    return _strict_tenant_predicate(table)


def _strict_tenant_predicate(table: str) -> str:
    return f"""
        {_bypass_predicate()}
        OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
    """


def _tenant_catalog_predicate() -> str:
    return f"""
        {_bypass_predicate()}
        OR tenants.id::text = current_setting('app.current_tenant_id', true)
    """


def _user_owned_predicate(table: str) -> str:
    return f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM users
            WHERE users.id = {table}.user_id
              AND users.tenant_id::text = current_setting('app.current_tenant_id', true)
        )
    """


def _plaza_child_predicate(table: str) -> str:
    return f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM plaza_posts
            WHERE plaza_posts.id = {table}.post_id
              AND plaza_posts.tenant_id::text = current_setting('app.current_tenant_id', true)
        )
    """


def _skill_file_predicates() -> tuple[str, str]:
    using = f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM skills
            WHERE skills.id = skill_files.skill_id
              AND (
                  skills.tenant_id::text = current_setting('app.current_tenant_id', true)
                  OR (skills.tenant_id IS NULL AND skills.is_builtin IS TRUE)
              )
        )
    """
    check = f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM skills
            WHERE skills.id = skill_files.skill_id
              AND skills.tenant_id::text = current_setting('app.current_tenant_id', true)
        )
    """
    return using, check


def _external_identity_predicate() -> str:
    return f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM users
            WHERE users.id = external_identities.user_id
              AND users.tenant_id::text = current_setting('app.current_tenant_id', true)
        )
    """


def _participant_predicate() -> str:
    return f"""
        {_bypass_predicate()}
        OR (
            participants.type = 'user'
            AND EXISTS (
                SELECT 1
                FROM users
                WHERE users.id = participants.ref_id
                  AND users.tenant_id::text = current_setting('app.current_tenant_id', true)
            )
        )
        OR (
            participants.type = 'agent'
            AND EXISTS (
                SELECT 1
                FROM agents
                WHERE agents.id = participants.ref_id
                  AND agents.tenant_id::text = current_setting('app.current_tenant_id', true)
            )
        )
    """


def _agent_team_child_predicate(table: str) -> str:
    return f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM agent_teams
            WHERE agent_teams.id = {table}.team_id
              AND agent_teams.tenant_id::text = current_setting('app.current_tenant_id', true)
        )
    """


def _system_settings_predicate() -> str:
    quoted_keys = ", ".join(f"'{key}'" for key in _SECRET_SYSTEM_SETTING_KEYS)
    return f"""
        {_bypass_predicate()}
        OR system_settings.key <> ALL (ARRAY[{quoted_keys}]::text[])
    """


def _policy_predicates_for_table(table: str) -> tuple[str, str]:
    if table == "tenants":
        predicate = _tenant_catalog_predicate()
    elif table in {"notifications", "refresh_tokens"}:
        predicate = _user_owned_predicate(table)
    elif table in {"plaza_comments", "plaza_likes"}:
        predicate = _plaza_child_predicate(table)
    elif table == "skill_files":
        return _skill_file_predicates()
    elif table == "external_identities":
        predicate = _external_identity_predicate()
    elif table == "participants":
        predicate = _participant_predicate()
    elif table in {"agent_team_members", "agent_team_events"}:
        predicate = _agent_team_child_predicate(table)
    elif table == "feature_flags":
        predicate = "true"
    elif table == "system_settings":
        predicate = _system_settings_predicate()
    elif table == "identities":
        predicate = _bypass_predicate()
    elif table in PLATFORM_SHARED_TENANT_PREDICATES:
        using = f"""
            {_strict_tenant_predicate(table)}
            OR ({PLATFORM_SHARED_TENANT_PREDICATES[table]})
        """
        return using, _strict_tenant_predicate(table)
    else:
        predicate = _strict_tenant_predicate(table)
    return predicate, predicate


def apply_rls_policies(
    connection: Connection,
    tables: Sequence[str] = RLS_TENANT_TABLES,
    forced_tables: Sequence[str] = RLS_FORCED_TENANT_TABLES,
) -> None:
    """Enable RLS + tenant policy on every existing table in ``tables`` /
    ``forced_tables`` (the latter also get FORCE ROW LEVEL SECURITY).

    §9 P0 gap fix: ``bootstrap_database_to_head`` used to create the schema
    via ``metadata.create_all`` and stamp head WITHOUT ever running the
    ``add_row_level_security`` migration — every fresh deployment shipped
    with zero RLS policies. The policy shape below matches that migration
    exactly. Idempotent (duplicate policies are swallowed); tables that don't
    exist in this database are skipped.
    """
    if connection.dialect.name != "postgresql":  # RLS is a PostgreSQL feature
        return
    existing = set(inspect(connection).get_table_names())
    connection.execute(text("SELECT set_config('app.current_tenant_id', '', false)"))
    forced_table_set = set(forced_tables)
    for table in dict.fromkeys((*tables, *forced_tables)):
        if table not in existing:
            continue
        forced = table in forced_table_set
        connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        if forced:
            connection.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        using_predicate, check_predicate = _policy_predicates_for_table(table)
        connection.execute(
            text(
                f"""
                DO $$ BEGIN
                    CREATE POLICY tenant_isolation_{table} ON {table}
                        USING ({using_predicate})
                        WITH CHECK ({check_predicate});
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$
                """
            )
        )


def apply_config_revision_immutability(connection: Connection) -> None:
    """Install the revision snapshot guard skipped by create_all + stamp bootstrap."""

    if connection.dialect.name != "postgresql":
        return
    if "config_revisions" not in set(inspect(connection).get_table_names()):
        return
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION enforce_config_revision_immutability()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'config revision snapshots are immutable';
                END IF;
                IF ROW(
                    OLD.id, OLD.entity_type, OLD.entity_id, OLD.tenant_id, OLD.version,
                    OLD.content_hash, OLD.content, OLD.diff_from_prev, OLD.change_source,
                    OLD.changed_by_user_id, OLD.changed_by_agent_id, OLD.change_message,
                    OLD.parent_revision_id, OLD.rollback_of_revision_id, OLD.created_at
                ) IS DISTINCT FROM ROW(
                    NEW.id, NEW.entity_type, NEW.entity_id, NEW.tenant_id, NEW.version,
                    NEW.content_hash, NEW.content, NEW.diff_from_prev, NEW.change_source,
                    NEW.changed_by_user_id, NEW.changed_by_agent_id, NEW.change_message,
                    NEW.parent_revision_id, NEW.rollback_of_revision_id, NEW.created_at
                ) OR (OLD.is_active = false AND NEW.is_active = true) THEN
                    RAISE EXCEPTION 'config revision snapshots are immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    connection.execute(text("DROP TRIGGER IF EXISTS trg_config_revision_immutability ON config_revisions"))
    connection.execute(
        text(
            """
            CREATE TRIGGER trg_config_revision_immutability
            BEFORE UPDATE OR DELETE ON config_revisions
            FOR EACH ROW EXECUTE FUNCTION enforce_config_revision_immutability()
            """
        )
    )


def apply_hr_creation_blueprint_immutability(connection: Connection) -> None:
    """Install the confirmed HR blueprint guard skipped by create_all + stamp."""

    if connection.dialect.name != "postgresql":
        return
    if "hr_creation_drafts" not in set(inspect(connection).get_table_names()):
        return
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION enforce_hr_creation_blueprint_immutability()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF (OLD.status = 'superseded' AND OLD.status IS DISTINCT FROM NEW.status)
                   OR (
                        OLD.status IN ('confirmed', 'creating', 'provisioning', 'failed', 'completed', 'superseded')
                        AND (
                        OLD.blueprint_version IS DISTINCT FROM NEW.blueprint_version
                        OR OLD.blueprint_hash IS DISTINCT FROM NEW.blueprint_hash
                        OR OLD.blueprint_json IS DISTINCT FROM NEW.blueprint_json
                        )
                   )
                THEN
                    RAISE EXCEPTION 'confirmed HR blueprint is immutable'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    connection.execute(text("DROP TRIGGER IF EXISTS trg_hr_creation_blueprint_immutable ON hr_creation_drafts"))
    connection.execute(
        text(
            """
            CREATE TRIGGER trg_hr_creation_blueprint_immutable
            BEFORE UPDATE OF blueprint_version, blueprint_hash, blueprint_json, status
            ON hr_creation_drafts
            FOR EACH ROW
            EXECUTE FUNCTION enforce_hr_creation_blueprint_immutability()
            """
        )
    )


def apply_workflow_promotion_immutability(connection: Connection) -> None:
    """Install the proposal snapshot guard on create_all + stamp bootstraps."""

    if connection.dialect.name != "postgresql":
        return
    if "workflow_promotion_proposals" not in set(inspect(connection).get_table_names()):
        return
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION workflow_promotion_snapshot_immutable()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'workflow promotion proposal snapshots are immutable';
                END IF;
                IF OLD.status IS DISTINCT FROM NEW.status
                   AND NOT (
                       (OLD.status = 'pending' AND NEW.status IN ('approved','rejected','stale','withdrawn'))
                       OR (OLD.status = 'withdrawn' AND NEW.status = 'pending')
                   ) THEN
                    RAISE EXCEPTION 'invalid workflow promotion proposal transition';
                END IF;
                IF OLD.status IN ('approved','rejected','stale')
                   AND (
                       OLD.reviewer_user_id IS DISTINCT FROM NEW.reviewer_user_id
                       OR OLD.review_reason IS DISTINCT FROM NEW.review_reason
                       OR OLD.reviewed_at IS DISTINCT FROM NEW.reviewed_at
                   ) THEN
                    RAISE EXCEPTION 'terminal workflow promotion review is immutable';
                END IF;
                IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
                   OR OLD.agent_id IS DISTINCT FROM NEW.agent_id
                   OR OLD.run_id IS DISTINCT FROM NEW.run_id
                   OR OLD.root_session_id IS DISTINCT FROM NEW.root_session_id
                   OR OLD.requester_user_id IS DISTINCT FROM NEW.requester_user_id
                   OR OLD.proposal_hash IS DISTINCT FROM NEW.proposal_hash
                   OR OLD.run_evidence_hash IS DISTINCT FROM NEW.run_evidence_hash
                   OR OLD.definition_hash IS DISTINCT FROM NEW.definition_hash
                   OR OLD.definition_json IS DISTINCT FROM NEW.definition_json
                   OR OLD.run_evidence_json IS DISTINCT FROM NEW.run_evidence_json
                   OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                    RAISE EXCEPTION 'workflow promotion proposal snapshots are immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    connection.execute(
        text("DROP TRIGGER IF EXISTS trg_workflow_promotion_snapshot_immutable ON workflow_promotion_proposals")
    )
    connection.execute(
        text(
            """
            CREATE TRIGGER trg_workflow_promotion_snapshot_immutable
            BEFORE UPDATE OR DELETE ON workflow_promotion_proposals
            FOR EACH ROW EXECUTE FUNCTION workflow_promotion_snapshot_immutable()
            """
        )
    )


_AUDIT_EVIDENCE_TABLES: tuple[str, ...] = (
    "audit_logs",
    "security_audit_events",
)


def apply_audit_evidence_immutability(connection: Connection) -> None:
    """Install append-only guards skipped by create_all + stamp bootstrap."""

    if connection.dialect.name != "postgresql":
        return
    existing_tables = set(inspect(connection).get_table_names())
    guarded_tables = [table for table in _AUDIT_EVIDENCE_TABLES if table in existing_tables]
    if not guarded_tables:
        return
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION reject_audit_evidence_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'audit evidence is append-only: %', TG_TABLE_NAME
                    USING ERRCODE = '55000';
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    for table in guarded_tables:
        trigger_name = f"trg_{table}_immutable"
        truncate_trigger_name = f"trg_{table}_no_truncate"
        connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}"))
        connection.execute(
            text(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_audit_evidence_mutation()
                """
            )
        )
        connection.execute(text(f"DROP TRIGGER IF EXISTS {truncate_trigger_name} ON {table}"))
        connection.execute(
            text(
                f"""
                CREATE TRIGGER {truncate_trigger_name}
                BEFORE TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_evidence_mutation()
                """
            )
        )


def apply_session_v2_contracts(connection: Connection) -> None:
    """Install exact Session V2 event/writer guards on create_all bootstraps."""

    if connection.dialect.name != "postgresql":
        return
    existing = set(inspect(connection).get_table_names())
    required = {"chat_transcript_events", "runtime_tasks", "session_writer_epochs"}
    if not required <= existing:
        return

    connection.execute(
        text(
            """
            INSERT INTO session_writer_epochs
              (id,state,new_run_generation,allowed_existing_generations_json,enforcement_mode,version)
            VALUES ('global','legacy_open',1,'[1]'::jsonb,'observe',1)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    # Fresh bootstrap and Alembic upgrade install the exact same generated
    # definitions; there is no hand-maintained SQL matrix to drift.
    from app.services.session_event_contract import (
        SESSION_V2_AUTHORITY_TABLES,
        SESSION_V2_TRIGGER_FUNCTION_SIGNATURES,
        build_session_event_contract_function_sql,
        build_session_tenant_binding_function_sql,
        build_session_writer_epoch_function_sql,
    )

    connection.execute(text(build_session_event_contract_function_sql()))
    connection.execute(text(build_session_writer_epoch_function_sql()))
    connection.execute(text(build_session_tenant_binding_function_sql()))
    for function_signature in SESSION_V2_TRIGGER_FUNCTION_SIGNATURES:
        connection.execute(text(f"REVOKE EXECUTE ON FUNCTION {function_signature} FROM PUBLIC"))
    connection.execute(text("DROP TRIGGER IF EXISTS trg_session_event_v2_contract ON chat_transcript_events"))
    connection.execute(
        text(
            """
            CREATE TRIGGER trg_session_event_v2_contract
            BEFORE INSERT OR UPDATE ON chat_transcript_events
            FOR EACH ROW EXECUTE FUNCTION public.enforce_session_event_v2_contract()
            """
        )
    )
    connection.execute(text("DROP TRIGGER IF EXISTS trg_session_writer_epoch ON runtime_tasks"))
    connection.execute(
        text(
            """
            CREATE TRIGGER trg_session_writer_epoch
            BEFORE INSERT OR UPDATE ON runtime_tasks
            FOR EACH ROW EXECUTE FUNCTION public.enforce_session_writer_epoch()
            """
        )
    )
    for table in SESSION_V2_AUTHORITY_TABLES:
        if table not in existing:
            continue
        trigger = f"trg_{table}_tenant_binding"
        connection.execute(text(f'DROP TRIGGER IF EXISTS "{trigger}" ON "{table}"'))
        connection.execute(
            text(
                f'CREATE TRIGGER "{trigger}" BEFORE INSERT OR UPDATE ON "{table}" '
                "FOR EACH ROW EXECUTE FUNCTION public.enforce_session_v2_tenant_binding()"
            )
        )


class AlembicContextProtocol(Protocol):
    def configure(self, **kwargs) -> None: ...

    def begin_transaction(self): ...

    def run_migrations(self) -> None: ...


def should_bootstrap_database(connection: Connection) -> bool:
    """Return True when the database should be initialized from current metadata.

    We bootstrap in two cases:
    1. Truly empty database
    2. Database with app tables but without Alembic version tracking
    """
    tables = set(inspect(connection).get_table_names())
    if _ALEMBIC_VERSION_TABLE in tables:
        return False
    if not tables:
        return True
    return bool(tables & _CORE_APP_TABLES)


def prepare_runtime_schema(connection: Connection, metadata: MetaData) -> bool:
    """Create current metadata only for an unversioned runtime database.

    A versioned database belongs exclusively to Alembic.  Calling create_all on
    it can materialize tables from migrations that have not run yet, leaving
    table existence ahead of the mechanical version/evidence source.
    """

    if not should_bootstrap_database(connection):
        return False
    metadata.create_all(bind=connection)
    apply_rls_policies(connection)
    apply_config_revision_immutability(connection)
    apply_hr_creation_blueprint_immutability(connection)
    apply_workflow_promotion_immutability(connection)
    apply_audit_evidence_immutability(connection)
    apply_session_v2_contracts(connection)
    return True


def ensure_alembic_version_table_width(connection: Connection) -> None:
    """Ensure Alembic can stamp long, descriptive revision identifiers.

    Alembic's default version table uses VARCHAR(32). This repository already
    has descriptive revision ids longer than that, so PostgreSQL upgrades must
    create or widen the table before Alembic writes `alembic_version`.
    """

    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS alembic_version (
                version_num VARCHAR({_ALEMBIC_VERSION_NUM_LENGTH}) NOT NULL PRIMARY KEY
            )
            """
        )
    )
    if connection.dialect.name == "postgresql":
        connection.execute(
            text(
                f"""
                ALTER TABLE alembic_version
                ALTER COLUMN version_num TYPE VARCHAR({_ALEMBIC_VERSION_NUM_LENGTH})
                """
            )
        )


def bootstrap_database_to_head(connection: Connection, metadata: MetaData, heads: Sequence[str]) -> None:
    """Create the current schema and stamp Alembic heads into an unversioned DB."""
    metadata.create_all(bind=connection)
    # Stamping head skips every migration — including add_row_level_security.
    # Apply the RLS policies explicitly so fresh deployments are not born
    # without tenant isolation (§9 P0 gap fix).
    apply_rls_policies(connection)
    apply_config_revision_immutability(connection)
    apply_hr_creation_blueprint_immutability(connection)
    apply_workflow_promotion_immutability(connection)
    apply_audit_evidence_immutability(connection)
    apply_session_v2_contracts(connection)
    ensure_alembic_version_table_width(connection)
    connection.execute(text("DELETE FROM alembic_version"))
    for head in heads:
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:version_num)"),
            {"version_num": head},
        )


def run_migrations_with_bootstrap(
    connection: Connection,
    *,
    alembic_context: AlembicContextProtocol,
    metadata: MetaData,
    heads: Sequence[str],
    should_bootstrap_fn=should_bootstrap_database,
    bootstrap_fn=bootstrap_database_to_head,
) -> None:
    """Either bootstrap an unversioned DB or run the normal Alembic path."""
    had_transaction = connection.in_transaction()
    if should_bootstrap_fn(connection):
        bootstrap_fn(connection, metadata, heads)
        if connection.in_transaction() and not had_transaction:
            connection.commit()
        return

    ensure_alembic_version_table_width(connection)
    # The version-table probe/ALTER autobegins a SQLAlchemy transaction.  End
    # that bootstrap transaction before handing control to Alembic; otherwise
    # Alembic treats it as an externally owned transaction and cannot open the
    # autocommit blocks required by rolling-safe data migrations.
    if connection.in_transaction() and not had_transaction:
        connection.commit()
    # Release trains can contain several data-heavy revisions.  Commit each
    # revision independently so an index/backfill in a later revision cannot
    # retain an earlier revision's ACCESS EXCLUSIVE DDL lock for the whole
    # upgrade-to-head transaction.
    alembic_context.configure(
        connection=connection,
        target_metadata=metadata,
        transaction_per_migration=True,
    )
    with alembic_context.begin_transaction():
        alembic_context.run_migrations()
    if connection.in_transaction() and not had_transaction:
        connection.commit()
