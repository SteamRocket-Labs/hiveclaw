"""Hive Backend — FastAPI Application Entry Point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.activity import router as activity_router
from app.api.admin import router as admin_router
from app.api.advanced import router as advanced_router
from app.api.agents import router as agents_router
from app.api.atlassian import router as atlassian_router
from app.api.auth import router as auth_router
from app.api.capabilities import router as capabilities_router
from app.api.chat_sessions import router as chat_sessions_router
from app.api.config_history import router as config_history_router
from app.api.desktop_agents import router as desktop_agents_router
from app.api.desktop_audit import router as desktop_audit_router
from app.api.desktop_auth import router as desktop_auth_router
from app.api.desktop_sync import router as desktop_sync_router
from app.api.dingtalk import router as dingtalk_router
from app.api.discord_bot import router as discord_router
from app.api.email_channel import router as email_channel_router
from app.api.enterprise import router as enterprise_router
from app.api.feature_flags import router as feature_flags_router
from app.api.feishu import router as feishu_router
from app.api.files import enterprise_kb_router
from app.api.files import router as files_router
from app.api.files import upload_router as files_upload_router
from app.api.gateway import router as gateway_router
from app.api.guard_policies import router as guard_policies_router
from app.api.llm_proxy import router as llm_proxy_router
from app.api.memory import router as memory_router
from app.api.messages import router as messages_router
from app.api.notification import router as notification_router
from app.api.oidc import router as oidc_router
from app.api.onboarding import router as onboarding_router
from app.api.organization import router as org_router
from app.api.packs import router as packs_router
from app.api.plaza import router as plaza_router
from app.api.relationships import router as relationships_router
from app.api.role_templates import router as role_templates_router
from app.api.schedules import router as schedules_router
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

settings = get_settings()


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
        nodes = [{"server": os.environ["SS_SERVER"], "port": int(os.environ.get("SS_PORT", "1080")),
                  "password": os.environ["SS_PASSWORD"], "method": os.environ.get("SS_METHOD", "chacha20-ietf-poly1305"), "label": "env"}]
    else:
        logger.info(f"[Proxy] {cfg_file} not found and SS_SERVER not set — skipping proxy")
        return
    for node in nodes:
        cfg = {"server": node["server"], "server_port": node["port"], "local_address": "127.0.0.1",
               "local_port": 1080, "password": node["password"], "method": node["method"], "timeout": 10}
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(cfg, tf)
        tf.close()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ss-local", "-c", tf.name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    # Configure logging first
    configure_logging()
    intercept_standard_logging()
    logger.info("[startup] Logging configured")

    import asyncio
    import os
    from app.services.trigger_daemon import start_trigger_daemon
    from app.services.tool_seeder import seed_builtin_tools
    from app.services.feishu_ws import feishu_ws_manager
    from app.services.dingtalk_stream import dingtalk_stream_manager
    from app.services.wecom_stream import wecom_stream_manager
    from app.services.wechat_personal_stream import wechat_personal_stream_manager

    # ── Step 0a: Validate production secrets ──
    if not settings.DEBUG:
        if settings.SECRET_KEY == "change-me-in-production":
            import logging as _log
            _log.getLogger(__name__).critical("SECRET_KEY has default value — set a strong random key for production")
        if settings.JWT_SECRET_KEY == "change-me-jwt-secret":
            import logging as _log
            _log.getLogger(__name__).critical("JWT_SECRET_KEY has default value — set a strong random key for production")

    # ── Step 0b: Initialize secrets provider ──
    from app.services.secrets_provider import init_secrets_provider
    init_secrets_provider(settings.SECRETS_MASTER_KEY or None)

    # ── Step 0c: Ensure all DB tables exist (idempotent, safe to run on every startup) ──
    try:
        from app.database import Base, engine
        # Import all models so Base.metadata is fully populated
        import app.models.user           # noqa
        import app.models.agent          # noqa
        import app.models.task           # noqa
        import app.models.llm_provider_config  # noqa: must be before llm
        import app.models.llm            # noqa
        import app.models.tool           # noqa
        import app.models.audit          # noqa
        import app.models.skill          # noqa
        import app.models.channel_config  # noqa
        import app.models.schedule       # noqa
        import app.models.plaza          # noqa
        import app.models.activity_log   # noqa
        import app.models.org            # noqa
        import app.models.system_settings  # noqa
        import app.models.invitation_code  # noqa
        import app.models.tenant         # noqa
        import app.models.tenant_setting  # noqa
        import app.models.participant    # noqa
        import app.models.chat_session   # noqa
        import app.models.trigger        # noqa
        import app.models.notification   # noqa
        import app.models.gateway_message # noqa
        import app.models.feature_flag    # noqa
        import app.models.security_audit  # noqa
        import app.models.capability_policy  # noqa
        import app.models.capability_install  # noqa
        import app.models.refresh_token  # noqa
        import app.models.guard_policy  # noqa
        import app.models.tenant_channel_config  # noqa
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Add enum values to channel_type_enum if they don't exist yet (idempotent)
            for _ch_val in ('atlassian', 'telegram'):
                await conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS '{_ch_val}'"
                    )
                )
        logger.info("[startup] Database tables ready")
    except Exception as e:
        logger.warning(f"[startup] create_all failed: {e}")

    # One-time workspace migration: update HEARTBEAT.md + remove deprecated skills
    try:
        from app.tools.workspace import migrate_all_workspaces
        migrate_all_workspaces()
    except Exception as e:
        logger.warning(f"[startup] workspace migration failed (non-fatal): {e}")

    # Startup: seed data — each step isolated so one failure doesn't block others
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

    # Migrate old shared enterprise_info/ → enterprise_info_{first_tenant_id}/
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
                        print(f"[startup] ℹ️ enterprise_info_{_tenant.id} already exists, skipping migration", flush=True)
    except Exception as e:
        print(f"[startup] ⚠️ enterprise_info migration failed: {e}", flush=True)

    try:
        await seed_builtin_tools()
    except Exception as e:
        logger.warning(f"[startup] Builtin tools seed failed: {e}")

    try:
        from app.agents.orchestrator import resume_persisted_async_delegations
        from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks
        resumed_task_ids = await resume_persisted_async_delegations(limit=50)
        if resumed_task_ids:
            logger.info("[startup] Resumed %d persisted async runtime task(s)", len(resumed_task_ids))
        reconciled = await reconcile_orphaned_runtime_tasks(exclude_task_ids=set(resumed_task_ids))
        if reconciled:
            logger.warning("[startup] Reconciled %d orphaned runtime task(s) after restart", reconciled)
    except Exception as e:
        logger.warning(f"[startup] Runtime task reconciliation failed: {e}")

    try:
        from app.services.tool_seeder import seed_atlassian_rovo_config, get_atlassian_api_key
        await seed_atlassian_rovo_config()
        # Auto-import Atlassian Rovo tools if an API key is already configured
        _rovo_key = await get_atlassian_api_key()
        if _rovo_key:
            from app.services.resource_discovery import seed_atlassian_rovo_tools
            await seed_atlassian_rovo_tools(_rovo_key)
    except Exception as e:
        logger.warning(f"[startup] Atlassian tools seed failed: {e}")

    try:
        from app.services.skill_seeder import (
            cleanup_retired_builtin_skills,
            push_default_skills_to_existing_agents,
            seed_skills,
        )
        await seed_skills()
        await cleanup_retired_builtin_skills()
        await push_default_skills_to_existing_agents()
    except Exception as e:
        logger.warning(f"[startup] Skills seed failed: {e}")

    try:
        from app.services.agent_seeder import seed_default_agents
        await seed_default_agents()
    except Exception as e:
        logger.warning(f"[startup] Default agents seed failed: {e}")

    # Register memory system hooks
    try:
        from app.runtime.hooks_setup import register_memory_hooks

        register_memory_hooks()
    except Exception as e:
        logger.warning(f"[startup] Memory hooks registration failed: {e}")

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
                logger.error(f"[startup] Background task {t.get_name()} CRASHED: {exc}")
                import traceback
                traceback.print_exception(type(exc), exc, exc.__traceback__)

        for name, coro in [
            ("trigger_daemon", start_trigger_daemon()),
            ("feishu_ws", feishu_ws_manager.start_all()),
            ("dingtalk_stream", dingtalk_stream_manager.start_all()),
            ("wecom_stream", wecom_stream_manager.start_all()),
            ("wechat_personal_stream", wechat_personal_stream_manager.start_all()),
        ]:
            task = asyncio.create_task(coro, name=name)
            task.add_done_callback(_bg_task_error)
            logger.info(f"[startup] created bg task: {name}")
        logger.info("[startup] all background tasks created!")
    except Exception as e:
        logger.error(f"[startup] Background tasks failed: {e}")
        import traceback
        traceback.print_exc()

    # Start ss-local SOCKS5 proxy for Discord API calls (non-fatal)
    asyncio.create_task(_start_ss_local(), name="ss-local-proxy")

    yield

    # Shutdown
    await wechat_personal_stream_manager.stop_all()
    await close_redis()
    try:
        from app.services.viking_client import close as close_viking
        await close_viking()
    except Exception as exc:
        logger.debug(f"OpenViking client cleanup skipped: {exc}")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
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

# All API routers — mounted under both /api (backward compat) and /api/v1
_api_routers = [
    auth_router, agents_router, tasks_router, files_router, feishu_router,
    org_router, enterprise_router, advanced_router, upload_router,
    relationships_router, activity_router, messages_router, tenants_router,
    schedules_router, files_upload_router, enterprise_kb_router,
    skills_router, users_router, slack_router, discord_router, dingtalk_router,
    wecom_router, wechat_personal_router, teams_router, telegram_router, email_channel_router, atlassian_router, notification_router,
    gateway_router, config_history_router, feature_flags_router, admin_router,
    chat_sessions_router, plaza_router, triggers_router, memory_router,
    oidc_router, capabilities_router, onboarding_router, packs_router,
    llm_proxy_router,
    desktop_auth_router,
    desktop_sync_router,
    desktop_agents_router,
    guard_policies_router,
    desktop_audit_router,
    role_templates_router,
    tenant_channels_router,
    tools_router,
]

for _r in _api_routers:
    app.include_router(_r, prefix="/api")      # backward compat
    app.include_router(_r, prefix="/api/v1")   # versioned

# Routers without /api prefix (WebSocket, webhooks, etc.)
app.include_router(webhooks_router)  # Public endpoint, no API prefix
app.include_router(ws_router)


# Health check — unversioned (infrastructure)
@app.get("/api/health", response_model=HealthResponse, tags=["health"])
async def health_check():
    """Health check endpoint."""
    return HealthResponse(status="ok", version=settings.APP_VERSION)
