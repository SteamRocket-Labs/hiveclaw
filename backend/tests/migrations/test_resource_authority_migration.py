from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_resource_authority_upgrade_contract(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "workspace_columns": {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("workspace_resource_manifests")
                    },
                    "artifact_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("chat_artifacts")
                    },
                    "task_columns": {column["name"] for column in inspect(sync_connection).get_columns("tasks")},
                    "activity_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("agent_activity_logs")
                    },
                    "workspace_uniques": {
                        item["name"]
                        for item in inspect(sync_connection).get_unique_constraints("workspace_resource_manifests")
                    },
                }
            )
        assert {
            "tenant_id",
            "agent_id",
            "path",
            "owner_user_id",
            "root_session_id",
            "authority_state",
            "source",
            "content_hash",
            "deleted_at",
        } <= schema["workspace_columns"]
        assert {"owner_user_id", "root_session_id", "authority_state"} <= schema["artifact_columns"]
        assert {"root_session_id", "authority_state"} <= schema["task_columns"]
        assert {"owner_user_id", "root_session_id", "authority_state"} <= schema["activity_columns"]
        assert "uq_workspace_resource_manifest_agent_path" in schema["workspace_uniques"]
    finally:
        await engine.dispose()


def test_resource_authority_migration_backfills_known_owners_and_quarantines_unknown_rows() -> None:
    migration = (Path(__file__).resolve().parents[2] / "alembic" / "versions" / "resource_authority_0711.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "resource_authority_0711"' in migration
    assert 'down_revision = "ai_asset_usage_events_0711"' in migration
    assert "chat_sessions" in migration
    assert "owner_user_id" in migration
    assert "root_session_id" in migration
    assert "quarantined" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_isolation_workspace_resource_manifests" in migration
    assert "BOOL_AND(artifact.owner_user_id IS NOT NULL)" in migration
    assert "COUNT(DISTINCT artifact.owner_user_id) = 1" in migration
    assert "artifact_authority" in migration
    assert r"replace(activity.detail_json::text, '\u0000', '\uFFFD')::jsonb" in migration


async def test_resource_authority_upgrade_tolerates_legacy_json_unicode_null(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"resource_authority_nul_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic_upgrade(database_url, "head")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "ai_asset_usage_events_0711"],
        cwd=Path(__file__).resolve().parents[2],
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
    session_id = uuid.uuid4()
    activity_id = uuid.uuid4()
    valid_activity_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            # Production migrations run with schema-owner visibility. Mirror
            # that visibility so the dirty legacy row is actually evaluated.
            await connection.execute(text("ALTER TABLE agent_activity_logs DISABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE chat_sessions DISABLE ROW LEVEL SECURITY"))
            await connection.execute(
                text(
                    """
                    INSERT INTO tenants (
                        id, name, slug, im_provider, is_active,
                        min_heartbeat_interval_minutes, timezone,
                        default_max_triggers, min_poll_interval_floor, max_webhook_rate_ceiling,
                        tokens_used_today, tokens_used_month, tokens_used_total, sync_version
                    ) VALUES (
                        :tenant_id, 'Legacy NUL Tenant', :slug, 'web_only', true,
                        45, 'Asia/Shanghai', 20, 5, 5, 0, 0, 0, 1
                    )
                    """
                ),
                {"tenant_id": tenant_id, "slug": f"legacy-nul-{tenant_id.hex[:8]}"},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO users (
                        id, username, email, password_hash, display_name, role,
                        tenant_id, is_active, must_change_password,
                        tokens_used_today, tokens_used_month, tokens_used_total
                    ) VALUES (
                        :user_id, :username, :email, '!', 'Legacy NUL User',
                        'member', :tenant_id, true, false, 0, 0, 0
                    )
                    """
                ),
                {
                    "user_id": user_id,
                    "username": f"legacy_nul_{user_id.hex[:8]}",
                    "email": f"legacy-nul-{user_id.hex[:8]}@example.com",
                    "tenant_id": tenant_id,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO participants (id, type, ref_id, display_name)
                    VALUES (:participant_id, 'agent', :agent_id, 'Legacy NUL Agent')
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
                        :agent_id, 'Legacy NUL Agent', '', :user_id, :user_id, :participant_id,
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
                    INSERT INTO chat_sessions (
                        id, agent_id, tenant_id, user_id, title, source_channel,
                        session_kind, actor_type, runtime_source, visibility_scope, listed_surface
                    ) VALUES (
                        :session_id, :agent_id, :tenant_id, :user_id, 'Unrelated Session', 'web',
                        'human_chat', 'user', 'web_chat', 'direct_user', 'chat'
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
                    INSERT INTO agent_activity_logs (
                        id, agent_id, tenant_id, action_type, summary, detail_json
                    ) VALUES (
                        :activity_id, :agent_id, :tenant_id, 'chat_reply',
                        'Legacy JSON Unicode NUL', CAST(:detail_json AS json)
                    )
                    """
                ),
                {
                    "activity_id": activity_id,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "detail_json": json.dumps({"session_id": "\u0000"}),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO agent_activity_logs (
                        id, agent_id, tenant_id, action_type, summary, detail_json
                    ) VALUES (
                        :activity_id, :agent_id, :tenant_id, 'chat_reply',
                        'Valid legacy session authority', CAST(:detail_json AS json)
                    )
                    """
                ),
                {
                    "activity_id": valid_activity_id,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "detail_json": json.dumps({"session_id": str(session_id)}),
                },
            )
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "resource_authority_0711")

    verify_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with verify_engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT owner_user_id, root_session_id, authority_state "
                            "FROM agent_activity_logs WHERE id IN (:activity_id, :valid_activity_id) "
                            "ORDER BY id"
                        ),
                        {"activity_id": activity_id, "valid_activity_id": valid_activity_id},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await verify_engine.dispose()

    assert {row["authority_state"] for row in rows} == {"owned", "quarantined"}
    owned = next(row for row in rows if row["authority_state"] == "owned")
    quarantined = next(row for row in rows if row["authority_state"] == "quarantined")
    assert owned["owner_user_id"] == user_id
    assert owned["root_session_id"] == session_id
    assert quarantined["owner_user_id"] is None
    assert quarantined["root_session_id"] is None
