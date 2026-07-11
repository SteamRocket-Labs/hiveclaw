from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.migrations.conftest import BACKEND_ROOT, _alembic_upgrade, _current_head_parent
from tests.integration.conftest import _async_url


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "external_principals_0711.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("external_principals_0711", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_external_principal_migration_contract_and_backfill_are_explicit():
    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "external_principals_0711"
    assert module.down_revision == "channel_ingress_inbox_0711"
    assert set(module._EXTERNAL_PRINCIPAL_RLS_TABLES) == {
        "external_principals",
        "external_principal_binding_events",
    }
    assert "tenant_id, provider, installation_ref, subject_id" in source
    assert "external_principal_id" in source
    assert "@slack.local" in source
    assert "@telegram.local" in source
    assert "@discord.local" in source
    assert "@teams.local" in source
    assert "@wecom.local" in source
    assert "@wechat.local" in source
    assert "@dingtalk.local" in source
    assert "SET is_active = false" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source


async def test_external_principal_upgrade_surface(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "principal_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("external_principals")
                    },
                    "session_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("chat_sessions")
                    },
                    "message_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("chat_messages")
                    },
                    "approval_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("approval_requests")
                    },
                    "budget_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("runtime_budget_runs")
                    },
                }
            )
            rls_rows = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('external_principals','external_principal_binding_events')"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    assert {"external_principals", "external_principal_binding_events"} <= schema["tables"]
    assert {
        "tenant_id",
        "provider",
        "installation_ref",
        "subject_id",
        "linked_user_id",
        "status",
        "profile_json",
        "last_seen_at",
    } <= schema["principal_columns"]
    assert "external_principal_id" in schema["session_columns"]
    assert "external_principal_id" in schema["message_columns"]
    assert "requested_by_external_principal_id" in schema["approval_columns"]
    assert "root_external_principal_id" in schema["budget_columns"]
    assert sorted(rls_rows) == [
        ("external_principal_binding_events", True, True),
        ("external_principals", True, True),
    ]


def test_fresh_bootstrap_forces_external_principal_rls():
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, STRICT_TENANT_RLS_TABLES

    expected = {"external_principals", "external_principal_binding_events"}
    assert expected <= set(RLS_FORCED_TENANT_TABLES)
    assert expected <= set(STRICT_TENANT_RLS_TABLES)


async def test_upgrade_backfills_legacy_channel_history_without_licensed_user_pollution(pg_container) -> None:
    database = f"externalbackfill_{uuid.uuid4().hex[:12]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f'CREATE DATABASE "{database}"'])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database).render_as_string(hide_password=False)
    # Fresh bootstrap intentionally stamps current metadata. Rewind through the
    # migration's own downgrade to obtain an exact parent schema, then seed the
    # pre-release rows and execute the real upgrade path.
    _alembic_upgrade(database_url, "head")
    parent = _current_head_parent()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", parent],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout={result.stdout[-2000:]} stderr={result.stderr[-2000:]}"

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    config_id = uuid.uuid4()
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO tenants (
                        id, name, slug, im_provider, is_active,
                        min_heartbeat_interval_minutes, timezone,
                        default_max_triggers, min_poll_interval_floor, max_webhook_rate_ceiling,
                        tokens_used_today, tokens_used_month, tokens_used_total, sync_version
                    ) VALUES (
                        :tenant_id, 'Legacy External Tenant', :slug, 'web_only', true,
                        45, 'Asia/Shanghai', 20, 5, 5, 0, 0, 0, 1
                    )
                    """
                ),
                {"tenant_id": tenant_id, "slug": f"legacy-external-{tenant_id.hex[:8]}"},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO users (
                        id, username, email, password_hash, display_name, role,
                        tenant_id, is_active, must_change_password,
                        tokens_used_today, tokens_used_month, tokens_used_total
                    ) VALUES (
                        :user_id, 'slack_U123', 'U123@slack.local', '!', 'Legacy Slack Guest',
                        'member', :tenant_id, true, false, 0, 0, 0
                    )
                    """
                ),
                {"user_id": user_id, "tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO participants (id, type, ref_id, display_name)
                    VALUES (:participant_id, 'agent', :agent_id, 'Legacy Agent')
                    """
                ),
                {"participant_id": participant_id, "agent_id": agent_id},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO agents (
                        id, name, role_description, creator_id, sponsor_user_id, participant_id,
                        tenant_id, agent_type, agent_class, security_zone, status,
                        tokens_used_today, tokens_used_month, tokens_used_total,
                        context_window_size, max_tool_rounds, execution_mode,
                        max_triggers, min_poll_interval_min, webhook_rate_limit,
                        heartbeat_enabled, heartbeat_interval_minutes, heartbeat_active_hours,
                        channel_perms, config_version, subagent_evolution_auto_approve
                    ) VALUES (
                        :agent_id, 'Legacy Agent', '', :user_id, :user_id, :participant_id,
                        :tenant_id, 'native', 'internal_tenant', 'standard', 'idle',
                        0, 0, 0, 100, 200, 'standard', 20, 5, 5, true, 45, '00:00-23:59',
                        false, 1, false
                    )
                    """
                ),
                {
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "participant_id": participant_id,
                    "tenant_id": tenant_id,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO approval_requests (
                        id, agent_id, tenant_id, action_type, details, status,
                        requested_by, execution_status
                    ) VALUES (
                        :approval_id, :agent_id, :tenant_id, 'workspace.file.write', '{}'::json,
                        'approved', :user_id, 'approved'
                    )
                    """
                ),
                {
                    "approval_id": approval_id,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO channel_configs (
                        id, agent_id, tenant_id, channel_type, is_configured, is_connected, extra_config
                    ) VALUES (:config_id, :agent_id, :tenant_id, 'slack', true, true, '{}'::json)
                    """
                ),
                {"config_id": config_id, "agent_id": agent_id, "tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO chat_sessions (
                        id, agent_id, tenant_id, user_id, title, source_channel, external_conv_id,
                        delivery_target_json, session_kind, actor_type, runtime_source,
                        visibility_scope, listed_surface
                    ) VALUES (
                        :session_id, :agent_id, :tenant_id, :user_id, 'Legacy Slack', 'slack',
                        'slack:C1:U123', '{"sender_id":"U123","channel_id":"C1"}'::jsonb,
                        'human_chat', 'user', 'channel_chat', 'direct_user', 'chat'
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO chat_messages (
                        id, agent_id, tenant_id, user_id, role, content, conversation_id
                    ) VALUES (
                        :message_id, :agent_id, :tenant_id, :user_id, 'user', 'legacy hello', :conversation_id
                    )
                    """
                ),
                {
                    "message_id": message_id,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "conversation_id": str(session_id),
                },
            )
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET LOCAL app.current_tenant_id = 'BYPASS'"))
            principals = (
                await connection.execute(
                    text(
                        "SELECT id, provider, installation_ref, subject_id, status "
                        "FROM external_principals WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
            ).all()
            if not principals:
                debug = (
                    await connection.execute(
                        text(
                            "SELECT "
                            "current_setting('app.current_tenant_id', true) AS scope, "
                            "(SELECT count(*) FROM users WHERE id = :user_id) AS users_seen, "
                            "(SELECT count(*) FROM chat_sessions WHERE id = :session_id) AS sessions_seen, "
                            "(SELECT is_active FROM users WHERE id = :user_id) AS user_active, "
                            "(SELECT count(*) FROM chat_sessions s JOIN users u ON u.id = s.user_id "
                            " WHERE s.id = :session_id AND u.email LIKE '%@slack.local') AS source_matches"
                        ),
                        {"user_id": user_id, "session_id": session_id},
                    )
                ).one()
                raise AssertionError(f"external principal backfill produced no rows: {debug}")
            principal = principals[0]
            session_projection = (
                await connection.execute(
                    text("SELECT user_id, external_principal_id FROM chat_sessions WHERE id = :id"),
                    {"id": session_id},
                )
            ).one()
            message_projection = (
                await connection.execute(
                    text("SELECT user_id, external_principal_id FROM chat_messages WHERE id = :id"),
                    {"id": message_id},
                )
            ).one()
            legacy_user_active = (
                await connection.execute(text("SELECT is_active FROM users WHERE id = :id"), {"id": user_id})
            ).scalar_one()
            approval_projection = (
                await connection.execute(
                    text(
                        "SELECT requested_by, requested_by_external_principal_id, status, execution_status, details "
                        "FROM approval_requests WHERE id = :id"
                    ),
                    {"id": approval_id},
                )
            ).one()
    finally:
        await engine.dispose()

    assert principal.provider == "slack"
    assert principal.installation_ref == str(config_id)
    assert principal.subject_id == "U123"
    assert principal.status == "active"
    assert session_projection.user_id is None
    assert session_projection.external_principal_id == principal.id
    assert message_projection.user_id is None
    assert message_projection.external_principal_id == principal.id
    assert legacy_user_active is False
    assert approval_projection.requested_by is None
    assert approval_projection.requested_by_external_principal_id == principal.id
    assert approval_projection.status == "rejected"
    assert approval_projection.execution_status == "needs_reapproval"
    assert approval_projection.details["legacy_external_identity_previous_status"] == "approved"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", parent],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout={result.stdout[-2000:]} stderr={result.stderr[-2000:]}"
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET LOCAL app.current_tenant_id = 'BYPASS'"))
            restored_session_user = (
                await connection.execute(
                    text("SELECT user_id FROM chat_sessions WHERE id = :id"),
                    {"id": session_id},
                )
            ).scalar_one()
            restored_message_user = (
                await connection.execute(
                    text("SELECT user_id FROM chat_messages WHERE id = :id"),
                    {"id": message_id},
                )
            ).scalar_one()
            restored_user_active = (
                await connection.execute(text("SELECT is_active FROM users WHERE id = :id"), {"id": user_id})
            ).scalar_one()
            restored_approval = (
                await connection.execute(
                    text(
                        "SELECT requested_by, status, execution_status, details FROM approval_requests WHERE id = :id"
                    ),
                    {"id": approval_id},
                )
            ).one()
    finally:
        await engine.dispose()

    assert restored_session_user == user_id
    assert restored_message_user == user_id
    assert restored_user_active is True
    assert restored_approval.requested_by == user_id
    assert restored_approval.status == "approved"
    assert restored_approval.execution_status == "approved"
    assert "legacy_external_identity_reconciliation" not in restored_approval.details
