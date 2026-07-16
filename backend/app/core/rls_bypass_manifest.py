"""Static, expiring authorization manifest for every RLS bypass callsite.

The scanner deliberately fingerprints the enclosing function, the exact reason
expression, and the SQLAlchemy query/write shape.  Adding a bypass or widening
its query therefore fails CI until platform-security explicitly registers it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RLSBypassCallsite:
    file: str
    function: str
    reason_expression: str
    allowed_query_fields: tuple[str, ...]

    @property
    def signature(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.file, self.function, self.reason_expression, self.allowed_query_fields)


@dataclass(frozen=True, slots=True)
class RLSBypassGrant(RLSBypassCallsite):
    owner: str
    expires_on: date


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def scan_rls_bypass_callsites(app_root: Path) -> list[RLSBypassCallsite]:
    callsites: list[RLSBypassCallsite] = []
    for path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "enter_rls_bypass":
                continue
            current: ast.AST = node
            function = "<module>"
            bypass_scope: ast.AST | None = None
            while current in parents:
                current = parents[current]
                if bypass_scope is None and isinstance(current, (ast.With, ast.AsyncWith)):
                    bypass_scope = current
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function = current.name
                    break

            reason = next(
                (ast.unparse(keyword.value) for keyword in node.keywords if keyword.arg == "reason"),
                "",
            )
            query_fields: list[str] = []
            for nested in ast.walk(bypass_scope or node):
                if not isinstance(nested, ast.Call):
                    continue
                operation = _call_name(nested)
                if operation not in {"select", "insert", "update", "delete"}:
                    continue
                arguments = ",".join(ast.unparse(argument) for argument in nested.args)
                query_fields.append(f"{operation}:{arguments}")
            if not query_fields:
                query_fields.append("session-state-only")

            callsites.append(
                RLSBypassCallsite(
                    file=path.relative_to(app_root.parent).as_posix(),
                    function=function,
                    reason_expression=reason,
                    allowed_query_fields=tuple(dict.fromkeys(query_fields)),
                )
            )
    return callsites


def _grant(
    file: str,
    function: str,
    reason_expression: str,
    allowed_query_fields: tuple[str, ...],
) -> RLSBypassGrant:
    return RLSBypassGrant(
        file=file,
        function=function,
        reason_expression=reason_expression,
        allowed_query_fields=allowed_query_fields,
        owner="platform-security",
        expires_on=date(2026, 10, 31),
    )


RLS_BYPASS_ALLOWLIST = (
    _grant(*("app/api/admin.py", "_load_platform_admin_agent_and_pin", "reason", ("select:Agent",))),
    _grant(
        *(
            "app/api/admin.py",
            "list_companies",
            "'platform admin company list and cross-tenant stats'",
            (
                "select:sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0)",
                "select:Tenant",
                "select:sqla_func.count()",
            ),
        )
    ),
    _grant(
        *(
            "app/scripts/audit_tenant_null_semantics.py",
            "audit_tenant_null_semantics",
            "'tenant NULL semantics read-only audit'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/scripts/repair_knowledge_provenance.py",
            "_main",
            "'append-only cross-tenant Knowledge provenance repair'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/scripts/storage_lifecycle.py",
            "_authoritative_agent_tenants",
            "'storage lifecycle fleet Agent tenant authority inventory'",
            ("select:Agent.id,Agent.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/api/admin.py",
            "get_metrics_timeseries",
            "'platform admin metrics timeseries cross-tenant aggregation'",
            (
                "select:sqla_func.coalesce(sqla_func.sum(TokenUsageEvent.tokens), 0)",
                "select:sqla_func.date(Tenant.created_at).label('d'),sqla_func.count().label('cnt')",
                "select:sqla_func.date(User.created_at).label('d'),sqla_func.count().label('cnt')",
                "select:sqla_func.date(TokenUsageEvent.created_at).label('d'),sqla_func.coalesce(sqla_func.sum(TokenUsageEvent.tokens), 0).label('tokens')",
                "select:sqla_func.count()",
            ),
        )
    ),
    _grant(
        *(
            "app/api/admin.py",
            "get_metrics_leaderboards",
            "'platform admin metrics leaderboard cross-tenant aggregation'",
            (
                "select:Agent.name,Tenant.name.label('company'),agent_tokens.label('tokens')",
                "select:Tenant.name,sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0).label('tokens')",
            ),
        )
    ),
    _grant(
        *(
            "app/api/auth.py",
            "register",
            "'public registration uniqueness and bootstrap checks'",
            ("select:User", "select:func.count()", "select:Tenant"),
        )
    ),
    _grant(
        *(
            "app/services/startup_bootstrap.py",
            "ensure_default_tenant",
            "'startup default tenant bootstrap'",
            ("insert:Tenant",),
        )
    ),
    _grant(*("app/api/auth.py", "login", "'public login identifier lookup'", ("select:User", "select:Tenant"))),
    _grant(
        *(
            "app/api/channel_rls.py",
            "load_public_agent_channel_config",
            "f'public {channel_type} webhook channel lookup for agent {agent_id}'",
            ("select:ChannelConfig", "select:Agent.tenant_id"),
        )
    ),
    _grant(
        *(
            "app/api/feishu.py",
            "_load_public_sso_scan_session",
            "f'public feishu sso session lookup for {session_id}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/api/feishu.py",
            "feishu_card_callback",
            "f'public feishu card approval lookup for {approval_uuid}'",
            ("select:ApprovalRequest", "select:AgentModel"),
        )
    ),
    _grant(*("app/api/tenants.py", "self_create_company", "'self-service company creation'", ("session-state-only",))),
    _grant(
        *(
            "app/api/tenants.py",
            "join_company",
            "'tenant join invitation lookup'",
            ("select:Tenant", "select:InvitationCode", "select:sqla_func.count()"),
        )
    ),
    _grant(*("app/api/tenants.py", "list_tenants", "'platform-admin list tenants'", ("select:Tenant",))),
    _grant(
        *(
            "app/api/tenants.py",
            "assign_user_to_tenant",
            "'platform-admin assign user to tenant'",
            ("select:Tenant", "select:User"),
        )
    ),
    _grant(*("app/api/tenants.py", "_platform_admin_bypass_scope", "reason", ("session-state-only",))),
    _grant(
        *(
            "app/api/users.py",
            "update_user_quota",
            "f'platform-admin user quota target lookup for {user_id}'",
            ("select:User",),
        )
    ),
    _grant(
        *(
            "app/api/webhooks.py",
            "receive_webhook",
            "'public webhook token resolution (tenant unknown until trigger found)'",
            ("select:AgentTrigger",),
        )
    ),
    _grant(
        *(
            "app/core/permissions.py",
            "check_agent_access",
            "f'platform-admin agent access lookup for {agent_id}'",
            ("select:Agent",),
        )
    ),
    _grant(
        *(
            "app/core/security.py",
            "verify_refresh_token",
            "'refresh token lookup'",
            ("select:RefreshToken", "select:User.tenant_id"),
        )
    ),
    _grant(
        *(
            "app/core/security.py",
            "revoke_refresh_token",
            "'refresh token revoke lookup'",
            ("select:RefreshToken", "select:User.tenant_id"),
        )
    ),
    _grant(
        *(
            "app/core/security.py",
            "get_current_user",
            "'platform-admin identity lookup before selected-tenant override'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/core/security.py",
            "get_current_user",
            "'tenantless authenticated identity lookup before company bootstrap'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/scripts/backfill_mcp_tool_names.py",
            "_run",
            "'Step 6 MCP canonical name backfill'",
            ("select:Tool", "update:Tool"),
        )
    ),
    _grant(
        *(
            "app/scripts/cleanup_duplicate_feishu_users.py",
            "main",
            "'feishu identity maintenance: backfill+merge users across all tenants'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/scripts/migrate_schedules_to_triggers.py",
            "migrate",
            "'one-time schedule→trigger migration across all agents'",
            (
                "select:AgentSchedule.id,AgentSchedule.agent_id,AgentSchedule.name,AgentSchedule.instruction,AgentSchedule.cron_expr,AgentSchedule.is_enabled,AgentSchedule.run_count,AgentSchedule.last_run_at",
                "select:AgentTrigger",
            ),
        )
    ),
    _grant(
        *(
            "app/scripts/repair_false_tool_evidence_notices.py",
            "_main",
            "'audited cross-tenant repair of exact retired tool-evidence verifier notices'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/agent_identity_lifecycle.py",
            "ensure_agent_identity",
            "rls_bypass_reason",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/agent_seeder.py",
            "seed_default_agents",
            "'first-startup default-agent seeding: resolve platform admin + seed Morty/Meeseeks'",
            ("select:SystemSetting", "select:Agent", "select:Skill", "select:Tool", "select:User"),
        )
    ),
    _grant(
        *(
            "app/services/approval_ticket.py",
            "consume_approval_ticket",
            "'approval ticket tenant locator'",
            ("select:ApprovalRequest.tenant_id,ApprovalRequest.agent_id",),
        )
    ),
    _grant(
        *(
            "app/services/approval_ticket.py",
            "reconcile_stuck_approval_tickets",
            "'approval ticket execution reconciliation locator'",
            ("select:ApprovalRequest.id,ApprovalRequest.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/business_task_runtime.py",
            "finalize_business_task_execution",
            "'business task finalization locator'",
            ("select:RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/channel_ingress_inbox.py",
            "_worker_session",
            "f'channel_ingress_inbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/channel_delivery_outbox.py",
            "_worker_session",
            "f'channel_delivery_outbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/budget_transition_outbox.py",
            "_worker_session",
            "f'budget_transition_outbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/business_task_runtime.py",
            "mark_business_task_execution_started",
            "'business task runtime locator'",
            ("select:RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/code_execution/probe.py",
            "store_latest_sandbox_probe_evidence",
            "'code execution sandbox probe latest evidence write'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/code_execution/probe.py",
            "latest_sandbox_probe_health",
            "'code execution sandbox probe latest evidence health read'",
            ("select:SystemSetting",),
        )
    ),
    _grant(
        *(
            "app/services/decision_trace.py",
            "_tenant_for_decision",
            "f'decision feedback tenant resolution for {normalized_id}'",
            ("select:DecisionTraceRecord.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/dingtalk_stream.py",
            "start_all",
            "'dingtalk start_all — enumerate all configured channels across tenants'",
            ("select:ChannelConfig",),
        )
    ),
    _grant(
        *(
            "app/services/evolution_daemon.py",
            "_drain_personal_kb_jobs",
            "'personal-kb import job drain — recover stale queued/running person-scope jobs'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/evolution_daemon.py",
            "_heartbeat_loop",
            "'pending-reply expiry sweep — expire stale contexts across all tenants'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/feishu_ws.py",
            "start_all",
            "'feishu start_all — enumerate all configured channels across tenants'",
            ("select:ChannelConfig",),
        )
    ),
    _grant(
        *(
            "app/services/heartbeat.py",
            "_run_chat_artifact_snapshot_retention",
            "'chat artifact snapshot retention across tenants'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/heartbeat.py",
            "_heartbeat_tick",
            "'heartbeat tick — enumerate all running/idle agents across tenants'",
            ("select:Agent",),
        )
    ),
    _grant(
        *(
            "app/services/heartbeat.py",
            "_workspace_full_sweep",
            "'workspace full sweep — enumerate active tenants across all agents'",
            ("select:Agent.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/hook_runtime_config.py",
            "apply_all_persisted_hook_runtime_configs",
            "'hook runtime config startup load'",
            ("select:SystemSetting",),
        )
    ),
    _grant(
        *(
            "app/services/local_agent_channel_service.py",
            "resolve_ws_ticket",
            "'local agent channel ws ticket lookup'",
            ("select:LocalAgentChannelWsTicket",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "_load_pairing_by_user_code",
            "'local bridge user-code pairing lookup'",
            ("select:LocalAgentBridgePairingSession",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "_load_pairing_by_device_code",
            "'local bridge device-code pairing lookup'",
            ("select:LocalAgentBridgePairingSession",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "resolve_bridge_auth_context",
            "'local bridge bearer token lookup'",
            ("select:LocalAgentBridgeConnection",),
        )
    ),
    _grant(
        *(
            "app/services/plan_mode_cutover.py",
            "mark_existing_triggers_plan_exempt",
            "'plan-mode cutover grandfather (cross-tenant trigger exemption)'",
            ("select:AgentTrigger",),
        )
    ),
    _grant(
        *(
            "app/services/plugin_hook_service.py",
            "register_installed_plugin_hooks",
            "'plugin hook startup registration'",
            ("select:PluginHookRegistration.tenant_id",),
        )
    ),
    _grant(
        *("app/services/resource_discovery.py", "_load", "'global ModelScope API token config read'", ("select:Tool",))
    ),
    _grant(*("app/services/resource_discovery.py", "_load", "'global Smithery API key config read'", ("select:Tool",))),
    _grant(
        *(
            "app/services/runtime_budget_service.py",
            "_budget_session",
            "f'runtime_budget_service.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_notification_outbox.py",
            "_worker_session",
            "f'runtime_notification_outbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/team_fanout_recovery.py",
            "_worker_session",
            "f'team_fanout_recovery.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_control_bus.py",
            "sweep_pending_transcript_t0_bridges",
            "'sweep pending transcript T0 projections'",
            ("select:ChatTranscriptEvent.id",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_service.py",
            "list_active_runtime_task_records",
            "'restart-safe async-delegation resume scan'",
            ("select:RuntimeTask.id,RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_service.py",
            "reconcile_orphaned_runtime_tasks",
            "'startup orphaned runtime-task reconcile'",
            ("select:RuntimeTask.id,RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/hr_creation_reconciliation.py",
            "reconcile_hr_creation_drafts_once",
            "'HR draft expiry and orphaned provisioning reconciliation'",
            ("select:HrCreationDraft.id,HrCreationDraft.provisioning_task_id",),
        )
    ),
    _grant(
        *(
            "app/services/business_task_reconciliation.py",
            "reconcile_stale_business_tasks_once",
            "'BusinessTask expired worker lease reconciliation'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "claim_and_dispatch_once",
            "'runtime task worker claim pending executable tasks'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_control_inputs_once",
            "'runtime task worker recover stale Session V2 control inputs'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "expire_session_permission_requests_once",
            "'runtime task worker expire Session V2 permission requests'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_model_rounds_once",
            "'runtime task worker recover sealed Session V2 model rounds'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_terminal_outcomes_once",
            "'runtime task worker recover sealed Session V2 terminal outcomes'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_terminal_outcomes_once",
            "'runtime task worker recover Session V2 terminal candidates'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_turn_replacement_sagas_once",
            "'runtime task worker recover Session V2 turn replacement sagas'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_stale_session_input_admissions_once",
            "'runtime task worker recover stale Session V2 input admissions'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_input_dispatches_once",
            "'runtime task worker dispatch admitted Session V2 inputs'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/session_event_outbox.py",
            "_worker_session",
            "f'session_event_outbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/session_workspace_snapshot.py",
            "recover_workspace_restores_from_transcript",
            "'workspace restore crash recovery'",
            ("select:ChatTranscriptEvent.id",),
        )
    ),
    _grant(
        *(
            "app/services/audit_logger.py",
            "write_audit_log",
            "'operator system audit log insert'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/audit_logger.py",
            "write_platform_security_audit_event",
            "'operator platform security audit insert'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/skill_seeder.py",
            "seed_skills",
            "'startup builtin skill registry seed'",
            ("select:Skill",),
        )
    ),
    _grant(
        *(
            "app/services/skill_seeder.py",
            "cleanup_retired_builtin_skills",
            "'startup maintenance: remove retired builtin skills across all agent workspaces'",
            ("delete:skill", "select:Agent", "select:Skill"),
        )
    ),
    _grant(
        *(
            "app/services/skill_seeder.py",
            "push_default_skills_to_existing_agents",
            "'startup: push default skills to every existing agent across tenants'",
            ("select:Agent", "select:Skill"),
        )
    ),
    _grant(
        *(
            "app/services/subagent_wake_consumer.py",
            "drain_subagent_completion_wakes",
            "'subagent wake daemon — enumerate completion signals'",
            ("select:CoordinationSignal",),
        )
    ),
    _grant(
        *(
            "app/services/t0_logger.py",
            "backfill_missing_chat_transcript_t0",
            "'startup legacy chat transcript/T0 backfill enumerate'",
            ("select:func.count(ChatTranscriptEvent.id)",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_agent",
            "f'tenant resolution for agent {agent_uuid}'",
            ("select:Agent.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_plan",
            "f'tenant resolution for plan {plan_id}'",
            ("select:AgentPlanRequest.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_runtime_task",
            "f'tenant resolution for runtime task {task_uuid}'",
            ("select:RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_user",
            "f'tenant resolution for user {user_uuid}'",
            ("select:User.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_chat_session",
            "f'tenant resolution for chat session {session_uuid}'",
            ("select:ChatSession.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_transcript_event",
            "f'tenant resolution for transcript event {event_uuid}'",
            ("select:ChatTranscriptEvent.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tool_seeder.py",
            "seed_builtin_tools",
            "'startup builtin-tool seeding: upsert platform tools + auto-assign to all agents'",
            ("select:Agent.id", "delete:obsolete", "select:Tool"),
        )
    ),
    _grant(
        *(
            "app/services/trigger_daemon.py",
            "_tick",
            "'trigger daemon tick — enumerate all enabled triggers across tenants'",
            ("select:AgentTrigger",),
        )
    ),
    _grant(
        *(
            "app/services/trigger_daemon.py",
            "backfill_null_reply_contexts",
            '"trigger reply_context backfill — enumerate all tenants\' enabled triggers"',
            ("select:AgentTrigger", "select:ChatSession"),
        )
    ),
    _grant(
        *(
            "app/services/wechat_personal_stream.py",
            "start_all",
            "'wechat_personal start_all — enumerate all connected channels across tenants'",
            ("select:ChannelConfig",),
        )
    ),
    _grant(
        *(
            "app/services/wecom_stream.py",
            "start_all",
            "'wecom start_all — enumerate all configured channels across tenants'",
            ("select:ChannelConfig",),
        )
    ),
)
