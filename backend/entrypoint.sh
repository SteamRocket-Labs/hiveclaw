#!/bin/bash
# Docker entrypoint: initialize DB tables, then start the app.
# Order matters:
#   1. create_all  - creates all tables using SQLAlchemy models (idempotent)
#   2. alembic stamp head - tells alembic we are at the latest revision (skips migrations)
#      For existing installs that may have missing columns, safe ALTER TABLE patches run first.
#   3. uvicorn - starts the FastAPI app

set -e

# Fix volume permissions (Railway mounts volumes as root, app runs as hive)
chown -R hive:hive /data 2>/dev/null || true

# Force git to use HTTPS instead of SSH (container has no SSH keys)
git config --global url."https://github.com/".insteadOf "git@github.com:"
git config --global url."https://github.com/".insteadOf "ssh://git@github.com/"

echo "[entrypoint] Step 1: Creating/verifying database tables..."

python << 'PYEOF'
import asyncio, sys

async def main():
    # Import all models to populate Base.metadata before create_all
    from app.database import Base, engine
    import app.models.user           # noqa
    import app.models.agent          # noqa
    import app.models.task           # noqa
    import app.models.llm            # noqa
    import app.models.tool           # noqa
    import app.models.audit          # noqa
    import app.models.skill          # noqa
    import app.models.channel_config # noqa
    import app.models.schedule       # noqa
    import app.models.plaza          # noqa
    import app.models.activity_log   # noqa
    import app.models.org            # noqa
    import app.models.system_settings # noqa
    import app.models.invitation_code # noqa
    import app.models.tenant         # noqa
    import app.models.participant     # noqa
    import app.models.chat_session   # noqa
    import app.models.trigger        # noqa
    import app.models.notification   # noqa
    import app.models.gateway_message # noqa
    import app.models.runtime_task    # noqa
    import app.models.capability_install  # noqa

    # Create all tables that don't exist yet (safe to run on every startup)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[entrypoint] Tables created/verified")

    # Apply safe column patches for existing installs that may be missing columns.
    # All statements use IF NOT EXISTS so they are fully idempotent.
    patches = [
        # Quota fields added in v0.2
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_message_limit INTEGER DEFAULT 50",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_message_period VARCHAR(20) DEFAULT 'permanent'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_messages_used INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_period_start TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_max_agents INTEGER DEFAULT 2",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_agent_ttl_hours INTEGER DEFAULT 48",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_calls_today INTEGER DEFAULT 0",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS max_llm_calls_per_day INTEGER DEFAULT 100",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_calls_reset_at TIMESTAMPTZ",
        # agent_tools source tracking added later
        "ALTER TABLE agent_tools ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'system'",
        "ALTER TABLE agent_tools ADD COLUMN IF NOT EXISTS installed_by_agent_id UUID",
        # chat_sessions channel tracking
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS source_channel VARCHAR(20) NOT NULL DEFAULT 'web'",
        # Token reset tracking
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_daily_reset TIMESTAMPTZ",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_monthly_reset TIMESTAMPTZ",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS tokens_used_total INTEGER DEFAULT 0",
        # OpenClaw Agent support
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_type VARCHAR(20) NOT NULL DEFAULT 'native'",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS openclaw_last_seen TIMESTAMPTZ",
        # Agent classification
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_class VARCHAR(30) DEFAULT 'general'",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS security_zone VARCHAR(30) DEFAULT 'standard'",
        # Fix security_audit_events sequence_num — make nullable (no PG sequence for non-PK)
        "ALTER TABLE security_audit_events ALTER COLUMN sequence_num DROP NOT NULL",
        # Memory service: session summary + model context window
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS summary TEXT",
        "ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS max_input_tokens INTEGER",
        # Agent status: add 'draft' to enum
        "ALTER TYPE agent_status_enum ADD VALUE IF NOT EXISTS 'draft'",
        # Invitation codes: tenant scoping
        "ALTER TABLE invitation_codes ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id)",
        "ALTER TABLE invitation_codes ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id)",
        # OIDC SSO
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub VARCHAR(255) UNIQUE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_issuer VARCHAR(500)",
        # Execution Identity (Block C)
        "ALTER TABLE security_audit_events ADD COLUMN IF NOT EXISTS execution_identity_type VARCHAR(20)",
        "ALTER TABLE security_audit_events ADD COLUMN IF NOT EXISTS execution_identity_id UUID",
        "ALTER TABLE security_audit_events ADD COLUMN IF NOT EXISTS execution_identity_label VARCHAR(200)",
        # Indexes for audit query (Block B)
        "CREATE INDEX IF NOT EXISTS ix_sec_audit_tenant_type_created ON security_audit_events (tenant_id, event_type, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_sec_audit_actor ON security_audit_events (actor_id)",
        "CREATE INDEX IF NOT EXISTS ix_sec_audit_resource ON security_audit_events (resource_type, resource_id)",
        # Drop dead autonomy_policy column (was stored but never enforced)
        "ALTER TABLE agents DROP COLUMN IF EXISTS autonomy_policy",
        # Context engineering sprint (2026-04-01): coordinator mode + runtime tasks
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS execution_mode VARCHAR(30) NOT NULL DEFAULT 'standard'",
    ]

    from sqlalchemy import text
    async with engine.begin() as conn:
        for sql in patches:
            try:
                await conn.execute(text(sql))
            except Exception as e:
                print(f"[entrypoint] Patch skipped ({e})")

    await engine.dispose()
    print("[entrypoint] Column patches applied")

asyncio.run(main())
PYEOF

echo "[entrypoint] Step 2: Running alembic migrations..."
# Run all migrations to ensure database schema is up to date
alembic upgrade head || echo "[entrypoint] WARNING: alembic migration failed (non-fatal, app may still work)"

echo "[entrypoint] Step 2.5: Running data migrations..."
# Safely migrate old AgentSchedules to the new AgentTriggers system
python -m app.scripts.migrate_schedules_to_triggers

# Step 2.7: Auto-authenticate lark-cli if Feishu app credentials are available
if [ -n "$FEISHU_APP_ID" ] && [ -n "$FEISHU_APP_SECRET" ] && command -v lark-cli >/dev/null 2>&1; then
    echo "[entrypoint] Step 2.7: Auto-authenticating lark-cli..."
    lark-cli auth login --app-id "$FEISHU_APP_ID" --app-secret "$FEISHU_APP_SECRET" 2>&1 || echo "[entrypoint] WARNING: lark-cli auth login failed (non-fatal)"
    # Auto-enable CLI if credentials succeeded
    if lark-cli auth status >/dev/null 2>&1; then
        export FEISHU_CLI_ENABLED=true
        echo "[entrypoint] lark-cli authenticated successfully, FEISHU_CLI_ENABLED=true"
    else
        echo "[entrypoint] lark-cli auth status check failed, CLI stays disabled"
    fi
else
    echo "[entrypoint] Skipping lark-cli auth (no FEISHU_APP_ID/SECRET or lark-cli not installed)"
fi

echo "[entrypoint] Step 3: Starting uvicorn..."
# Drop to hive user for the app process (entrypoint runs as root for volume chown)
if [ "$(id -u)" = "0" ] && id hive >/dev/null 2>&1; then
    exec su hive -s /bin/bash -c "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"
else
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'
fi
