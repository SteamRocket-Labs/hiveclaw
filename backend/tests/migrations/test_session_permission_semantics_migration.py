from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "session_permission_semantics_0713.py"


def test_session_permission_semantics_migration_is_reversible_and_normalizes_legacy_grants():
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "session_permission_semantics_0713"' in source
    assert 'down_revision = "agent_session_permission_default_0713"' in source
    assert "chat_sessions" in source
    assert "runtime_tasks" in source
    assert "break_glass" in source
    assert "bypassPermissions" in source
    assert "default" in source
    assert "_session_permission_semantics_0713_backup" in source
    assert "def downgrade()" in source


def _alembic(database_url: str, command: str, target: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout={result.stdout[-2000:]} stderr={result.stderr[-2000:]}"


async def _load_metadata(database_url: str, session_id: uuid.UUID, task_id: uuid.UUID, agent_id: uuid.UUID):
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE chat_sessions DISABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY"))
            session_metadata = await connection.scalar(
                text("SELECT transcript_metadata_json FROM chat_sessions WHERE id = :id"),
                {"id": session_id},
            )
            task_metadata = await connection.scalar(
                text("SELECT metadata_json FROM runtime_tasks WHERE id = :id"),
                {"id": task_id},
            )
            agent_default = await connection.scalar(
                text("SELECT default_session_permission_mode FROM agents WHERE id = :id"),
                {"id": agent_id},
            )
            await connection.execute(text("ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE chat_sessions FORCE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY"))
        return session_metadata, task_metadata, agent_default
    finally:
        await engine.dispose()


async def test_session_permission_semantics_real_postgres_roundtrip(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"session_permission_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic_upgrade(database_url, "head")
    _alembic(database_url, "downgrade", "agent_session_permission_default_0713")

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    task_id = uuid.uuid4()
    legacy_session_metadata = {
        "permission_mode": "bypassPermissions",
        "permission_profile": {
            "mode": "bypassPermissions",
            "allowed_tools": ["read_file"],
            "writable_roots": ["workspace/"],
        },
        "break_glass": {
            "operator_id": str(user_id),
            "reason": "legacy incident response",
            "scope": "session",
            "expires_at": "2026-07-13T13:00:00+00:00",
        },
        "keep": "chat",
    }
    legacy_task_metadata = {
        "permission_mode": "bypassPermissions",
        "permission_profile": {"mode": "bypassPermissions", "allowed_tools": ["write_file"]},
        "break_glass": {
            "operator_id": str(user_id),
            "reason": "legacy task grant",
            "scope": "session",
            "expires_at": "2026-07-13T13:00:00+00:00",
        },
        "keep": "task",
    }

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE chat_sessions DISABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY"))
            await connection.execute(
                text(
                    """
                    INSERT INTO tenants (
                        id, name, slug, im_provider, is_active,
                        min_heartbeat_interval_minutes, timezone,
                        default_max_triggers, min_poll_interval_floor, max_webhook_rate_ceiling,
                        tokens_used_today, tokens_used_month, tokens_used_total, sync_version
                    ) VALUES (
                        :tenant_id, 'Permission Migration', :slug, 'web_only', true,
                        45, 'UTC', 20, 5, 5, 0, 0, 0, 1
                    )
                    """
                ),
                {"tenant_id": tenant_id, "slug": f"permission-{tenant_id.hex[:8]}"},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO users (
                        id, username, email, password_hash, display_name, role,
                        tenant_id, is_active, must_change_password,
                        tokens_used_today, tokens_used_month, tokens_used_total
                    ) VALUES (
                        :user_id, :username, :email, '!', 'Permission User',
                        'member', :tenant_id, true, false, 0, 0, 0
                    )
                    """
                ),
                {
                    "user_id": user_id,
                    "username": f"permission_{user_id.hex[:8]}",
                    "email": f"permission-{user_id.hex[:8]}@example.com",
                    "tenant_id": tenant_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO participants (id, type, ref_id, display_name) "
                    "VALUES (:participant_id, 'agent', :agent_id, 'Permission Agent')"
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
                        channel_perms, config_version, subagent_evolution_auto_approve,
                        default_session_permission_mode
                    ) VALUES (
                        :agent_id, 'Permission Agent', '', :user_id, :user_id, :participant_id,
                        :tenant_id, 'native', 'internal_tenant', 'standard', 'idle',
                        0, 0, 0, 100, 200, 'standard', 20, 5, 5, true, 45, '00:00-23:59',
                        false, 1, false, 'bypassPermissions'
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
                        session_kind, actor_type, runtime_source, visibility_scope, listed_surface,
                        transcript_metadata_json
                    ) VALUES (
                        :session_id, :agent_id, :tenant_id, :user_id, 'Legacy Full Access', 'web',
                        'human_chat', 'user', 'web_chat', 'direct_user', 'chat',
                        CAST(:metadata AS jsonb)
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "metadata": json.dumps(legacy_session_metadata),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO runtime_tasks (
                        id, tenant_id, task_type, status, depth, metadata_json,
                        root_idempotency_key, config_snapshot_hash, policy_snapshot_hash
                    ) VALUES (
                        :task_id, :tenant_id, 'web_chat_turn', 'completed', 1, CAST(:metadata AS json),
                        :root_key, :config_hash, :policy_hash
                    )
                    """
                ),
                {
                    "task_id": task_id,
                    "tenant_id": tenant_id,
                    "metadata": json.dumps(legacy_task_metadata),
                    "root_key": f"web_chat_turn:{task_id}",
                    "config_hash": "c" * 64,
                    "policy_hash": "p" * 64,
                },
            )
            await connection.execute(text("ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE chat_sessions FORCE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY"))
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "head")
    migrated_session, migrated_task, agent_default = await _load_metadata(database_url, session_id, task_id, agent_id)
    assert migrated_session["permission_mode"] == "default"
    assert migrated_session["permission_profile"]["mode"] == "default"
    assert migrated_session["permission_profile"]["allowed_tools"] == ["read_file"]
    assert "break_glass" not in migrated_session
    assert migrated_session["keep"] == "chat"
    assert "_session_permission_semantics_0713_backup" in migrated_session
    assert migrated_task["permission_mode"] == "default"
    assert migrated_task["permission_profile"]["mode"] == "default"
    assert "break_glass" not in migrated_task
    assert migrated_task["keep"] == "task"
    assert agent_default == "bypassPermissions"

    _alembic(database_url, "downgrade", "agent_session_permission_default_0713")
    restored_session, restored_task, restored_agent_default = await _load_metadata(
        database_url, session_id, task_id, agent_id
    )
    assert restored_session == legacy_session_metadata
    assert restored_task == legacy_task_metadata
    assert restored_agent_default == "bypassPermissions"

    _alembic_upgrade(database_url, "head")
    remigrated_session, remigrated_task, remigrated_agent_default = await _load_metadata(
        database_url, session_id, task_id, agent_id
    )
    assert remigrated_session["permission_mode"] == "default"
    assert remigrated_task["permission_mode"] == "default"
    assert remigrated_agent_default == "bypassPermissions"
