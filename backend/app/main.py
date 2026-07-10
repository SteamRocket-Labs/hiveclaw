"""Hive Backend — FastAPI Application Entry Point."""

import asyncio
from contextlib import asynccontextmanager
import re

from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.activity import router as activity_router
from app.api.ai_assets import router as ai_assets_router
from app.api.a2a import router as a2a_router
from app.api.admin import router as admin_router
from app.api.advanced import router as advanced_router
from app.api.agents import router as agents_router
from app.api.auth import router as auth_router
from app.api.autonomy import router as autonomy_router
from app.api.capabilities import router as capabilities_router
from app.api.chat_sessions import router as chat_sessions_router
from app.api.commands import router as commands_router
from app.api.config_history import router as config_history_router
from app.api.custom_api_connectors import router as custom_api_connectors_router
from app.api.desktop_agents import router as desktop_agents_router
from app.api.desktop_audit import router as desktop_audit_router
from app.api.desktop_auth import router as desktop_auth_router
from app.api.desktop_sync import router as desktop_sync_router
from app.api.dingtalk import router as dingtalk_router
from app.api.discord_bot import router as discord_router
from app.api.email_channel import router as email_channel_router
from app.api.enterprise import router as enterprise_router
from app.api.external_capabilities import router as external_capabilities_router
from app.api.feature_flags import router as feature_flags_router
from app.api.feishu import router as feishu_router
from app.api.files import enterprise_kb_router
from app.api.files import router as files_router
from app.api.files import upload_router as files_upload_router
from app.api.guard_policies import router as guard_policies_router
from app.api.hooks import router as hooks_router
from app.api.hr_creation import router as hr_creation_router
from app.api.interoperability import router as interoperability_router
from app.api.local_agent_channel import local_agent_browser_channel_ws
from app.api.local_agent_channel import router as local_agent_channel_router
from app.api.local_bridge import router as local_bridge_router
from app.api.workflows import router as workflows_router
from app.api.workflow_definitions import router as workflow_definitions_router
from app.api.agent_knowledge import personal_router as personal_knowledge_router
from app.api.agent_knowledge import router as agent_knowledge_router
from app.api.agent_subagents import enterprise_router as enterprise_subagents_router
from app.api.agent_subagents import router as agent_subagents_router
from app.api.llm_proxy import router as llm_proxy_router
from app.api.memory import router as memory_router
from app.api.metrics import router as metrics_router
from app.api.messages import router as messages_router
from app.api.notification import router as notification_router
from app.api.oidc import router as oidc_router
from app.api.onboarding import router as onboarding_router
from app.api.office import router as office_router
from app.api.mcp_servers import router as mcp_servers_router
from app.api.mcp_oauth import router as mcp_oauth_router
from app.api.plugins import router as plugins_router
from app.api.organization import router as org_router
from app.api.plans import router as plans_router
from app.api.plaza import router as plaza_router
from app.api.agent_teams import router as agent_teams_router
from app.api.advanced_plan import router as advanced_plan_router
from app.api.role_templates import router as role_templates_router
from app.api.runtime_budgets import router as runtime_budgets_router
from app.api.schedules import router as schedules_router
from app.api.session_goals import router as session_goals_router
from app.api.skills import router as skills_router
from app.api.slack import router as slack_router
from app.api.tasks import router as tasks_router
from app.api.teams import router as teams_router
from app.api.telegram import router as telegram_router
from app.api.tenant_channels import router as tenant_channels_router
from app.api.tenants import router as tenants_router
from app.api.tools import router as tools_router
from app.api.triggers import router as triggers_router
from app.api.upload import router as upload_router
from app.api.users import router as users_router
from app.api.webhooks import router as webhooks_router
from app.api.wechat_personal import router as wechat_personal_router
from app.api.wecom import router as wecom_router
from app.api.websocket import router as ws_router
from app.config import get_settings
from app.core.events import close_redis
from app.core.logging_config import configure_logging, intercept_standard_logging
from app.core.middleware import TraceIdMiddleware
from app.core.tenant_middleware import TenantMiddleware
from app.schemas.schemas import HealthResponse
from app.services.code_execution.probe import (
    latest_sandbox_probe_health,
    should_run_sandbox_probe_scheduler,
    start_code_execution_sandbox_probe_scheduler,
)

settings = get_settings()


def _channel_stream_startup_enabled() -> bool:
    return bool(settings.CHANNEL_STREAM_STARTUP_ENABLED)


def _core_daemon_startup_enabled() -> bool:
    return bool(settings.CORE_DAEMON_STARTUP_ENABLED)


def _process_role() -> str:
    return str(settings.HIVE_PROCESS_ROLE or "runtime").strip().lower()


def _runtime_execution_startup_enabled() -> bool:
    return _process_role() not in {"api", "read_model"}


def _volume_bound_startup_enabled() -> bool:
    return _process_role() != "api"


def _schema_bootstrap_startup_enabled() -> bool:
    return _process_role() != "api"


def _data_bootstrap_startup_enabled() -> bool:
    return _process_role() != "api"


_API_ROLE_PREFIXES = (
    "/api/auth/",
    "/api/v1/auth/",
    "/api/users/",
    "/api/v1/users/",
    "/api/tenants/",
    "/api/v1/tenants/",
    "/api/organization/",
    "/api/v1/organization/",
    "/api/feature-flags/",
    "/api/v1/feature-flags/",
    "/api/notification/",
    "/api/v1/notification/",
    "/api/notifications/",
    "/api/v1/notifications/",
    "/api/desktop-auth/",
    "/api/v1/desktop-auth/",
    "/api/desktop-sync/",
    "/api/v1/desktop-sync/",
)
_API_ROLE_EXACT_PATHS = {
    "/api/health",
    "/api/auth",
    "/api/v1/auth",
    "/api/users",
    "/api/v1/users",
    "/api/tenants",
    "/api/v1/tenants",
    "/api/organization",
    "/api/v1/organization",
    "/api/agents",
    "/api/v1/agents",
}
_API_ROLE_AGENT_PATTERNS = (
    re.compile(r"^/api/(v1/)?agents/[^/]+/?$"),
    re.compile(r"^/api/(v1/)?agents/[^/]+/sessions/?$"),
    re.compile(r"^/api/(v1/)?agents/[^/]+/sessions/[^/]+/runs(?:/.*)?$"),
    re.compile(r"^/api/(v1/)?agents/[^/]+/sessions/[^/]+/turns/steer/?$"),
    re.compile(r"^/api/(v1/)?agents/[^/]+/sessions/[^/]+/permissions/[^/]+/resolve/?$"),
    re.compile(r"^/api/(v1/)?agents/[^/]+/hr-creation-drafts/[^/]+(?:/(?:confirm|reject))?/?$"),
)


def _api_role_allows_path(path: str) -> bool:
    normalized = "/" + str(path or "").lstrip("/")
    exact_normalized = normalized.rstrip("/") or "/"
    if normalized.startswith(("/docs", "/redoc", "/openapi.json", "/ws/")):
        return True
    if exact_normalized in _API_ROLE_EXACT_PATHS:
        return True
    if any(normalized.startswith(prefix) for prefix in _API_ROLE_PREFIXES):
        return True
    return any(pattern.match(normalized) for pattern in _API_ROLE_AGENT_PATTERNS)


async def _start_ss_local() -> None:
    """Start ss-local SOCKS5 proxy for Discord API calls. Tries nodes in priority order."""
    import asyncio
    import json
    import os
    import shutil
    import tempfile

    if not shutil.which("ss-local"):
        logger.info("[Proxy] ss-local not found — Discord proxy disabled")
        return
    # Load proxy nodes from config file (gitignored, mounted as Docker volume)
    import json as _json

    cfg_file = os.environ.get("SS_CONFIG_FILE", "/data/ss-nodes.json")
    if os.path.exists(cfg_file):
        nodes = _json.load(open(cfg_file))
        logger.info(f"[Proxy] Loaded {len(nodes)} node(s) from {cfg_file}")
    elif os.environ.get("SS_SERVER") and os.environ.get("SS_PASSWORD"):
        nodes = [
            {
                "server": os.environ["SS_SERVER"],
                "port": int(os.environ.get("SS_PORT", "1080")),
                "password": os.environ["SS_PASSWORD"],
                "method": os.environ.get("SS_METHOD", "chacha20-ietf-poly1305"),
                "label": "env",
            }
        ]
    else:
        logger.info(f"[Proxy] {cfg_file} not found and SS_SERVER not set — skipping proxy")
        return
    for node in nodes:
        cfg = {
            "server": node["server"],
            "server_port": node["port"],
            "local_address": "127.0.0.1",
            "local_port": 1080,
            "password": node["password"],
            "method": node["method"],
            "timeout": 10,
        }
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(cfg, tf)
        tf.close()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ss-local", "-c", tf.name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            await asyncio.sleep(2)
            if proc.returncode is None:
                os.environ["DISCORD_PROXY"] = "socks5h://127.0.0.1:1080"
                logger.info(f"[Proxy] ss-local → {node['label']} ({node['server']}:{node['port']})")
                return
            err = (await proc.stderr.read()).decode()[:120]
            logger.warning(f"[Proxy] {node['label']} failed: {err}")
        except Exception as e:
            logger.error(f"[Proxy] {node['label']} error: {e}")
    logger.warning("[Proxy] All SS nodes failed — Discord API calls will run without proxy")


async def _resume_runtime_tasks_after_startup(done_event=None) -> None:
    """Resume/reconcile durable runtime work without blocking FastAPI health startup."""
    try:
        from app.agents.orchestrator import resume_persisted_async_delegations
        from app.api.chat_sessions import expire_stale_session_permission_requests
        from app.database import async_session as _session_permission_scan
        from app.services.heartbeat import resume_persisted_heartbeat_runs
        from app.services.approval_ticket import reconcile_stuck_approval_tickets
        from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks
        from app.services.subagent_run_service import resume_persisted_subagent_runs
        from app.services.trigger_daemon import resume_persisted_trigger_runs
        from app.services.web_chat_runtime import resume_persisted_web_chat_runs

        async with _session_permission_scan() as _db_permission_scan:
            expired_permissions = await expire_stale_session_permission_requests(db=_db_permission_scan)
            if expired_permissions:
                logger.info("[startup] Marked {} stale session permission request(s) expired", expired_permissions)
        if not _runtime_execution_startup_enabled():
            logger.info("[startup] runtime resume/reconcile disabled for process role {}", _process_role())
            return

        from datetime import datetime, timedelta, timezone

        reconciled_approvals = await reconcile_stuck_approval_tickets(
            older_than=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        if reconciled_approvals:
            logger.warning(
                "[startup] Marked {} interrupted approval execution(s) for reconciliation",
                reconciled_approvals,
            )

        resumed_task_ids = await resume_persisted_async_delegations(limit=50)
        resumed_subagent_ids = await resume_persisted_subagent_runs(limit=50)
        resumed_web_chat_ids = await resume_persisted_web_chat_runs(limit=50)
        resumed_trigger_ids = await resume_persisted_trigger_runs(limit=50)
        resumed_heartbeat_ids = await resume_persisted_heartbeat_runs(limit=50)
        resumed_task_ids = [
            *resumed_task_ids,
            *resumed_subagent_ids,
            *resumed_web_chat_ids,
            *resumed_trigger_ids,
            *resumed_heartbeat_ids,
        ]
        if resumed_task_ids:
            logger.info("[startup] Resumed {} persisted async runtime task(s)", len(resumed_task_ids))
        reconciled = await reconcile_orphaned_runtime_tasks(exclude_task_ids=set(resumed_task_ids))
        if reconciled:
            logger.warning("[startup] Reconciled {} orphaned runtime task(s) after restart", reconciled)
    except Exception as e:
        logger.warning(f"[startup] Runtime task reconciliation failed: {e}")
    finally:
        if done_event is not None:
            done_event.set()


async def _run_after_startup_resume_gate(done_event, coro) -> None:
    """Run a startup coroutine only after runtime resume/reconcile has released worker claim."""
    try:
        await done_event.wait()
        await coro
    except asyncio.CancelledError:
        if hasattr(coro, "close"):
            coro.close()
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    # Configure logging first
    configure_logging()
    intercept_standard_logging()
    logger.info("[startup] Logging configured")

    from app.services.event_loop_monitor import event_loop_lag_monitor

    event_loop_lag_monitor.start()

    import asyncio
    import os
    from app.services.evolution_daemon import start_evolution_daemon
    from app.services.runtime_budget_daemon import start_runtime_budget_daemon
    from app.services.trigger_daemon import start_trigger_daemon
    from app.services.tool_seeder import seed_builtin_tools
    from app.services.feishu_ws import feishu_ws_manager
    from app.services.dingtalk_stream import dingtalk_stream_manager
    from app.services.wecom_stream import wecom_stream_manager
    from app.services.wechat_personal_stream import wechat_personal_stream_manager
    from app.services.workflow_daemon import request_default_workflow_drain, start_workflow_daemon

    # ── Step 0a: Validate production secrets ──
    if not settings.DEBUG:
        if settings.SECRET_KEY == "change-me-in-production":
            import logging as _log

            _log.getLogger(__name__).critical("SECRET_KEY has default value — set a strong random key for production")
        if settings.JWT_SECRET_KEY == "change-me-jwt-secret":
            import logging as _log

            _log.getLogger(__name__).critical(
                "JWT_SECRET_KEY has default value — set a strong random key for production"
            )

    # ── Step 0b: Initialize secrets provider ──
    from app.services.secrets_provider import init_secrets_provider, validate_secrets_provider_config

    validate_secrets_provider_config(settings.SECRETS_MASTER_KEY or None, debug=settings.DEBUG)
    init_secrets_provider(settings.SECRETS_MASTER_KEY or None)

    # ── Step 0c: Ensure all DB tables exist (idempotent, safe to run on runtime startup) ──
    if _schema_bootstrap_startup_enabled():
        try:
            from app.database import Base, schema_engine
            from app.models import import_all_models

            import_all_models()

            # Schema bootstrap runs on the owner connection (schema_engine): after the
            # stage-3 role flip the app engine is the non-owner app_rls role, which
            # cannot create_all / apply policies. Pre-cutover schema_engine IS engine.
            async with schema_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                from app.db_bootstrap import apply_rls_policies

                await conn.run_sync(apply_rls_policies)
                # Add enum values to channel_type_enum if they don't exist yet (idempotent)
                for _ch_val in ("telegram",):
                    await conn.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS '{_ch_val}'"
                        )
                    )
            logger.info("[startup] Database tables ready")
        except Exception as e:
            logger.warning(f"[startup] create_all failed: {e}")
    else:
        logger.info("[startup] schema bootstrap skipped for no-volume API role")

    # Verify the application runtime database role after schema bootstrap. This
    # is deliberately outside the create_all try/except: strict enforcement must
    # fail startup when RLS cannot be trusted.
    from app.services.rls_runtime_guard import check_runtime_rls_role

    await check_runtime_rls_role(enforcement=settings.RLS_RUNTIME_ROLE_ENFORCEMENT)

    # One-time workspace migration: update HEARTBEAT.md + remove deprecated skills
    if _volume_bound_startup_enabled():
        try:
            from app.tools.workspace import migrate_all_workspaces

            migrate_all_workspaces()
        except Exception as e:
            logger.warning(f"[startup] workspace migration failed (non-fatal): {e}")
    else:
        logger.info("[startup] workspace migration skipped for no-volume API role")

    # Register concrete Plan Mode handoff handlers onto the REST API's shared
    # service so a confirmed scheduled_trigger plan actually creates its enabled
    # trigger (instead of resolving to "skipped").
    try:
        from app.api.plans import get_plan_mode_service
        from app.services.plan_mode_registry import register_plan_mode_handoffs

        register_plan_mode_handoffs(get_plan_mode_service())
        logger.info("[startup] Plan Mode handoff handlers registered")
    except Exception as e:
        logger.warning(f"[startup] Plan Mode handoff registration failed (non-fatal): {e}")

    # Startup: seed data — each step isolated so one failure doesn't block others.
    # No-volume API roles must stay request-focused; runtime backend performs DB/data bootstrap.
    if _data_bootstrap_startup_enabled():
        logger.info("[startup] seeding...")

        # Seed default company (Tenant) — required before users can register
        try:
            from app.models.tenant import Tenant
            from app.database import async_session as _session
            from sqlalchemy import select as _select

            async with _session() as _db:
                _existing = await _db.execute(_select(Tenant).where(Tenant.slug == "default"))
                if not _existing.scalar_one_or_none():
                    _db.add(Tenant(name="Default", slug="default", im_provider="web_only"))
                    await _db.commit()
                    logger.info("[startup] Default company created")
        except Exception as e:
            logger.warning(f"[startup] Default company seed failed: {e}")
    else:
        logger.info("[startup] data bootstrap seed skipped for no-volume API role")

    # Migrate old shared enterprise_info/ → enterprise_info_{first_tenant_id}/
    if _volume_bound_startup_enabled():
        try:
            import shutil
            from pathlib import Path as _Path
            from app.config import get_settings as _gs
            from app.models.tenant import Tenant as _T
            from app.database import async_session as _ses
            from sqlalchemy import select as _sel

            _data_dir = _Path(_gs().AGENT_DATA_DIR)
            _old_dir = _data_dir / "enterprise_info"
            if _old_dir.exists() and any(_old_dir.iterdir()):
                async with _ses() as _db:
                    _first = await _db.execute(_sel(_T).order_by(_T.created_at).limit(1))
                    _tenant = _first.scalar_one_or_none()
                    if _tenant:
                        _new_dir = _data_dir / f"enterprise_info_{_tenant.id}"
                        if not _new_dir.exists():
                            shutil.copytree(str(_old_dir), str(_new_dir))
                            print(f"[startup] ✅ Migrated enterprise_info → enterprise_info_{_tenant.id}", flush=True)
                        else:
                            print(
                                f"[startup] ℹ️ enterprise_info_{_tenant.id} already exists, skipping migration",
                                flush=True,
                            )
        except Exception as e:
            print(f"[startup] ⚠️ enterprise_info migration failed: {e}", flush=True)
    else:
        logger.info("[startup] enterprise_info volume migration skipped for no-volume API role")

    # ── MCP backfill (idempotent): legacy Tool(type=mcp) → server-first tables ──
    # The Skill+MCP cutover left this as a manual admin endpoint, so freshly
    # migrated tenants showed 0 MCP servers in the new extension UI even though
    # the agents still had them (runtime kept working via the legacy fallback).
    # Run it per-tenant on every startup; existing server_key rows are skipped,
    # so it is safe and cheap to re-run.
    if _data_bootstrap_startup_enabled():
        try:
            import importlib as _il_mcp
            import pkgutil as _pkg_mcp
            import app.models as _am_mcp

            for _mod in _pkg_mcp.iter_modules(_am_mcp.__path__):
                _il_mcp.import_module(f"app.models.{_mod.name}")
            from app.services.mcp_backfill_service import backfill_tenant_mcp_servers as _bf_mcp
            from app.models.tenant import Tenant as _T_mcp
            from app.database import async_session as _ses_mcp
            from sqlalchemy import select as _sel_mcp

            _bf_servers = 0
            async with _ses_mcp() as _db_mcp:
                _tenants_mcp = await _db_mcp.execute(_sel_mcp(_T_mcp))
                for _tenant_mcp in _tenants_mcp.scalars().all():
                    try:
                        _bf_r = await _bf_mcp(_db_mcp, _tenant_mcp.id)
                        _bf_servers += int(_bf_r.get("servers", 0))
                    except Exception as _bf_e:
                        logger.warning(f"[startup] MCP backfill failed for tenant {_tenant_mcp.id}: {_bf_e}")
            if _bf_servers:
                logger.info(f"[startup] MCP backfill: created {_bf_servers} new server(s)")
        except Exception as e:
            logger.warning(f"[startup] MCP backfill step failed (non-fatal): {e}")

        try:
            await seed_builtin_tools()
        except Exception as e:
            logger.warning(f"[startup] Builtin tools seed failed: {e}")
    else:
        logger.info("[startup] MCP/tool DB seed skipped for no-volume API role")

    # Hard invariant (Step 0): no CORE tool may also be a pack member. Fail-fast
    # at startup so CORE∩pack drift is caught at deploy time, not in production.
    # NOT wrapped in try/except — a violation must crash startup, not warn.
    from app.tools.audit import assert_core_pack_disjoint, assert_manifests_valid

    assert_core_pack_disjoint()
    assert_manifests_valid()

    # Run tool coverage audit — flag tools without any discovery path
    # (no pack, no declared_tools in a system/template skill, no prompt mention).
    try:
        from app.tools.audit import run_startup_audit

        run_startup_audit()
    except Exception as e:
        logger.warning(f"[startup] Tool coverage audit failed: {e}")

    # C9-1: crash recovery for T2 package jobs — stale queued/running manifests
    # from a dead process are normalized to held so the heartbeat sweep can
    # retry them. Zero-LLM state normalization only; retries run on heartbeat.
    if _volume_bound_startup_enabled():
        try:
            from pathlib import Path as _T2SweepPath

            from app.config import get_settings as _t2_sweep_settings
            from app.memory.t2.job_sweep import sweep_all_agents_stale_t2_jobs

            _t2_sweep_reports = sweep_all_agents_stale_t2_jobs(
                data_root=_T2SweepPath(_t2_sweep_settings().AGENT_DATA_DIR)
            )
            _t2_recovered = sum(len(report.recovered_stale) for report in _t2_sweep_reports)
            if _t2_recovered:
                logger.info("[startup] T2 job sweep: crash-recovered {} stale job(s) to held", _t2_recovered)
        except Exception as e:
            logger.warning(f"[startup] T2 job sweep failed (non-fatal): {e}")
    else:
        logger.info("[startup] T2 job sweep skipped for no-volume API role")

    if _data_bootstrap_startup_enabled():
        try:
            from app.services.skill_seeder import (
                cleanup_retired_builtin_skills,
                push_default_skills_to_existing_agents,
                seed_skills,
            )

            await seed_skills()
            if _volume_bound_startup_enabled():
                await cleanup_retired_builtin_skills()
                await push_default_skills_to_existing_agents()
            else:
                logger.info("[startup] skill workspace maintenance skipped for no-volume API role")
        except Exception as e:
            logger.warning(f"[startup] Skills seed failed: {e}")
    else:
        logger.info("[startup] skill DB seed skipped for no-volume API role")

    if _volume_bound_startup_enabled():
        try:
            from app.services.agent_seeder import seed_default_agents

            await seed_default_agents()
        except Exception as e:
            logger.warning(f"[startup] Default agents seed failed: {e}")
    else:
        logger.info("[startup] default agent workspace seed skipped for no-volume API role")

    # P1-W2-9: reconcile registered tools against the capability mapping.
    # Surfaces drift (new tools without policy, dead policy entries) at
    # boot rather than waiting for an unmapped tool to be invoked.
    try:
        from app.services.capability_gate import audit_capability_mapping

        audit_capability_mapping()
    except Exception as e:
        logger.warning(f"[startup] Capability mapping audit failed (non-fatal): {e}")

    # Register memory system hooks
    try:
        from app.runtime.hooks_setup import register_memory_hooks

        register_memory_hooks()
    except Exception as e:
        logger.warning(f"[startup] Memory hooks registration failed: {e}")

    try:
        from app.services.plugin_hook_service import register_installed_plugin_hooks

        await register_installed_plugin_hooks()
    except Exception as e:
        logger.warning(f"[startup] Plugin hooks registration failed: {e}")

    try:
        from app.services.hook_runtime_config import apply_all_persisted_hook_runtime_configs

        await apply_all_persisted_hook_runtime_configs()
    except Exception as e:
        logger.warning(f"[startup] Hook runtime config load failed: {e}")

    # Backfill reply_context for triggers created before the unified-delivery
    # refactor — those triggers have reply_context=NULL and cannot deliver
    # results back to TG/WeChat/Feishu channels.
    if _data_bootstrap_startup_enabled():
        try:
            from app.services.trigger_daemon import backfill_null_reply_contexts

            result = await backfill_null_reply_contexts()
            if result["patched"]:
                logger.info(
                    "[startup] Backfilled {} trigger reply_contexts (skipped {})", result["patched"], result["skipped"]
                )
        except Exception as e:
            logger.warning(f"[startup] Trigger reply_context backfill failed: {e}")
    else:
        logger.info("[startup] trigger reply_context backfill skipped for no-volume API role")

    # Start background tasks (always, even if seeding failed)
    try:
        logger.info("[startup] starting background tasks...")
        from app.services.audit_logger import write_audit_log

        await write_audit_log("server_startup", {"pid": os.getpid()})

        def _bg_task_error(t):
            """Callback to surface background task exceptions."""
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                logger.debug(f"[startup] Background task {t.get_name()} cancelled (expected during shutdown)")
                return
            if exc:
                from app.services.daemon_liveness import mark_daemon_crashed

                mark_daemon_crashed(t.get_name(), exc)
                logger.error(f"[startup] Background task {t.get_name()} CRASHED: {exc}")
                import traceback

                traceback.print_exception(type(exc), exc, exc.__traceback__)
                return
            if t.get_name() in {"trigger_daemon", "workflow_daemon", "evolution_daemon"}:
                from app.services.daemon_liveness import mark_daemon_stopped

                mark_daemon_stopped(t.get_name(), "background task exited")

        runtime_startup_resume_done = asyncio.Event()
        startup_background_tasks = [
            ("code_execution_sandbox_probe_scheduler", start_code_execution_sandbox_probe_scheduler()),
        ]
        if _runtime_execution_startup_enabled():
            startup_background_tasks.append(
                ("runtime_startup_resume", _resume_runtime_tasks_after_startup(runtime_startup_resume_done))
            )
            try:
                from app.services.runtime_control_bus import start_runtime_control_listener

                startup_background_tasks.append(("runtime_control_listener", start_runtime_control_listener()))
            except Exception as exc:
                logger.warning("[startup] runtime control listener setup failed: {}", exc)
        else:
            runtime_startup_resume_done.set()
            logger.info("[startup] runtime resume/reconcile disabled for process role {}", _process_role())
        try:
            from app.services.runtime_task_worker import runtime_task_worker_enabled, start_runtime_task_worker_loop

            if runtime_task_worker_enabled():
                startup_background_tasks.append(
                    (
                        "runtime_task_worker",
                        _run_after_startup_resume_gate(runtime_startup_resume_done, start_runtime_task_worker_loop()),
                    )
                )
            else:
                logger.info("[startup] runtime task worker disabled for process role {}", _process_role())
        except Exception as exc:
            logger.warning("[startup] runtime task worker setup failed: {}", exc)
        if _runtime_execution_startup_enabled():
            startup_background_tasks.append(("runtime_budget_daemon", start_runtime_budget_daemon()))
        if _process_role() == "api":
            from app.services.web_chat_stream_bus import start_web_chat_stream_forwarder

            startup_background_tasks.append(("web_chat_stream_forwarder", start_web_chat_stream_forwarder()))
        if _core_daemon_startup_enabled():
            startup_background_tasks.extend(
                [
                    ("trigger_daemon", start_trigger_daemon()),
                    ("workflow_daemon", start_workflow_daemon()),
                    ("evolution_daemon", start_evolution_daemon()),
                ]
            )
        else:
            logger.info("[startup] core daemon startup disabled")
        if _channel_stream_startup_enabled():
            startup_background_tasks.extend(
                [
                    ("feishu_ws", feishu_ws_manager.start_all()),
                    ("dingtalk_stream", dingtalk_stream_manager.start_all()),
                    ("wecom_stream", wecom_stream_manager.start_all()),
                    ("wechat_personal_stream", wechat_personal_stream_manager.start_all()),
                ]
            )
        else:
            logger.info("[startup] channel stream startup disabled")
        if settings.T0_STARTUP_BACKFILL_ENABLED and _volume_bound_startup_enabled():
            from app.services.t0_logger import run_startup_chat_transcript_t0_backfill

            startup_background_tasks.append(("t0_startup_backfill", run_startup_chat_transcript_t0_backfill()))

        for name, coro in startup_background_tasks:
            if name in {"trigger_daemon", "workflow_daemon", "evolution_daemon"}:
                from app.services.daemon_liveness import register_daemon

                register_daemon(name)
            task = asyncio.create_task(coro, name=name)
            task.add_done_callback(_bg_task_error)
            logger.info(f"[startup] created bg task: {name}")
        logger.info("[startup] all background tasks created!")
    except Exception as e:
        logger.error(f"[startup] Background tasks failed: {e}")
        import traceback

        traceback.print_exc()

    # Start ss-local SOCKS5 proxy for Discord API calls (non-fatal)
    if _volume_bound_startup_enabled():
        asyncio.create_task(_start_ss_local(), name="ss-local-proxy")
    else:
        logger.info("[startup] ss-local proxy skipped for no-volume API role")

    yield

    # Shutdown
    await event_loop_lag_monitor.stop()
    try:
        request_default_workflow_drain()
    except Exception as exc:
        logger.warning(f"Workflow daemon drain request failed: {exc}")
    await wechat_personal_stream_manager.stop_all()
    await close_redis()
    try:
        from app.services.viking_client import close as close_viking

        await close_viking()
    except Exception as exc:
        logger.debug(f"OpenViking client cleanup skipped: {exc}")
    try:
        from app.memory.backend import aclose_all_backends

        await aclose_all_backends()
    except Exception as exc:
        logger.warning(f"Memory backend shutdown failed: {exc}")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    # MB-scale payloads (workbench/transcript/export) serialize on the event
    # loop; orjson keeps that burst an order of magnitude shorter (plan A2).
    default_response_class=ORJSONResponse,
)

# Add TraceIdMiddleware first so it's executed for all requests
app.add_middleware(TraceIdMiddleware)

# CORS — reject wildcard in production
_cors_origins = settings.CORS_ORIGINS
if "*" in _cors_origins and not settings.DEBUG:
    import logging as _logging

    _logging.getLogger(__name__).critical(
        "CORS_ORIGINS contains '*' in non-DEBUG mode. "
        "Set explicit origins (e.g. CORS_ORIGINS='[\"https://your-domain.com\"]') for production."
    )
_allow_creds = "*" not in _cors_origins  # CORS spec forbids credentials with wildcard
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tenant isolation middleware (runs after CORS, extracts tenant_id from JWT)
app.add_middleware(TenantMiddleware)


@app.middleware("http")
async def api_role_runtime_boundary(request: Request, call_next):
    if _process_role() == "api" and not _api_role_allows_path(request.url.path):
        return ORJSONResponse(
            status_code=404,
            content={
                "detail": "Route is not served by the no-volume API role. Use the runtime/read-model backend route.",
                "process_role": "api",
            },
        )
    return await call_next(request)


# All API routers — mounted under both /api (backward compat) and /api/v1
_api_routers = [
    auth_router,
    agents_router,
    tasks_router,
    files_router,
    feishu_router,
    org_router,
    enterprise_router,
    advanced_router,
    upload_router,
    a2a_router,
    activity_router,
    messages_router,
    tenants_router,
    schedules_router,
    files_upload_router,
    enterprise_kb_router,
    skills_router,
    users_router,
    slack_router,
    discord_router,
    dingtalk_router,
    wecom_router,
    wechat_personal_router,
    teams_router,
    telegram_router,
    email_channel_router,
    notification_router,
    config_history_router,
    ai_assets_router,
    custom_api_connectors_router,
    feature_flags_router,
    admin_router,
    chat_sessions_router,
    commands_router,
    session_goals_router,
    agent_teams_router,
    advanced_plan_router,
    hooks_router,
    hr_creation_router,
    plaza_router,
    triggers_router,
    memory_router,
    oidc_router,
    capabilities_router,
    onboarding_router,
    mcp_servers_router,
    mcp_oauth_router,
    plugins_router,
    plans_router,
    office_router,
    autonomy_router,
    llm_proxy_router,
    desktop_auth_router,
    desktop_sync_router,
    desktop_agents_router,
    guard_policies_router,
    local_bridge_router,
    local_agent_channel_router,
    desktop_audit_router,
    role_templates_router,
    runtime_budgets_router,
    tenant_channels_router,
    external_capabilities_router,
    tools_router,
    workflows_router,
    workflow_definitions_router,
    personal_knowledge_router,
    agent_knowledge_router,
    agent_subagents_router,
    enterprise_subagents_router,
    interoperability_router,
]

for _r in _api_routers:
    app.include_router(_r, prefix="/api")  # backward compat
    app.include_router(_r, prefix="/api/v1")  # versioned

# Routers without /api prefix (WebSocket, webhooks, etc.)
app.include_router(webhooks_router)  # Public endpoint, no API prefix
app.include_router(ws_router)
app.add_api_websocket_route("/ws/local-agents/sessions/{session_id}", local_agent_browser_channel_ws)
app.include_router(metrics_router)


# Health check — unversioned (infrastructure)
@app.get("/api/health", response_model=HealthResponse, tags=["health"])
async def health_check():
    """Health check endpoint."""
    from app.database import snapshot_db_pool
    from app.services.daemon_liveness import daemon_health_status, daemon_liveness_snapshot
    from app.services.event_loop_monitor import event_loop_lag_monitor
    from app.services.rls_runtime_guard import latest_runtime_rls_role_health
    from app.services.runtime_control_bus import runtime_control_bus_snapshot
    from app.services.runtime_task_worker import runtime_task_worker_snapshot
    from app.services.web_chat_stream_bus import web_chat_stream_forwarder_snapshot

    db_pool = snapshot_db_pool()
    rls_runtime_role = latest_runtime_rls_role_health()
    try:
        sandbox_probe = await latest_sandbox_probe_health()
    except Exception as exc:
        sandbox_probe = {
            "status": "degraded",
            "latest_probe_present": False,
            "scheduler_enabled": should_run_sandbox_probe_scheduler(),
            "error": {"type": type(exc).__name__, "message": str(exc)[:500]},
        }
    status = daemon_health_status()
    if rls_runtime_role["status"] in {"critical", "degraded"} and status == "ok":
        status = "degraded"
    if (
        sandbox_probe["status"] in {"critical", "degraded"}
        and sandbox_probe.get("scheduler_enabled") is True
        and status == "ok"
    ):
        status = "degraded"
    if db_pool["saturation_pct"] >= 100.0 and status == "ok":
        status = "degraded"
    return HealthResponse(
        status=status,
        version=settings.APP_VERSION,
        components={
            "daemons": daemon_liveness_snapshot(),
            "rls_runtime_role": rls_runtime_role,
            "code_execution_sandbox_probe": sandbox_probe,
            "process_role": {"role": _process_role()},
            "runtime_control_bus": runtime_control_bus_snapshot(),
            "runtime_task_worker": runtime_task_worker_snapshot(),
            "web_chat_stream_forwarder": web_chat_stream_forwarder_snapshot(),
            "db_pool": db_pool,
            "event_loop": event_loop_lag_monitor.snapshot(),
        },
    )
