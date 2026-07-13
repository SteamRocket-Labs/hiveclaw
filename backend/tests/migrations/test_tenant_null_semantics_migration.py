from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "tenant_null_semantics_0712.py"

EXPECTED_LEGACY_NULLABLE_TENANT_OWNED_TABLES = {
    "agent_activity_logs",
    "agent_agent_relationships",
    "agent_capability_installs",
    "agent_permissions",
    "agent_plan_recommendations",
    "agent_plan_requests",
    "agent_relationships",
    "agent_schedules",
    "agent_session_goals",
    "agent_teams",
    "agent_tools",
    "agent_triggers",
    "agent_work_ledgers",
    "agents",
    "approval_requests",
    "channel_configs",
    "chat_artifacts",
    "chat_messages",
    "chat_sessions",
    "chat_transcript_events",
    "decision_trace_feedback",
    "decision_traces",
    "departments",
    "enterprise_info",
    "invitation_codes",
    "local_agent_bridge_pairing_sessions",
    "local_agent_channel_events",
    "local_agent_channel_messages",
    "local_agent_channel_sessions",
    "org_departments",
    "org_members",
    "pending_reply_contexts",
    "plaza_posts",
    "runtime_budget_events",
    "runtime_budget_runs",
    "runtime_tasks",
    "session_feedback_events",
    "sso_scan_sessions",
    "task_logs",
    "tasks",
    "token_usage_events",
    "workflow_leaf_calls",
    "workflow_quotas",
    "workflow_steps",
}


def test_tenant_null_semantics_migration_contract() -> None:
    import importlib.util

    from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID, TENANT_SCOPE_QUARANTINE_SLUG

    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "tenant_null_semantics_0712"' in source
    assert 'down_revision = "rls_complete_coverage_0712"' in source
    assert "tenant_scope_quarantine_records" in source
    assert "__hive_scope_quarantine__" in source
    assert "SET NOT NULL" in source
    assert "autocommit_block" in source
    assert "_set_tenant_not_null_online" in source
    assert "NOT VALID" in source
    assert "VALIDATE CONSTRAINT" in source
    assert "PLATFORM_SHARED" in source
    assert "WITH CHECK" in source
    assert "secure downgrade" in source.lower()
    assert "DROP TABLE tenant_scope_quarantine_records" not in source

    spec = importlib.util.spec_from_file_location("tenant_null_semantics_contract", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.BACKFILL_SOURCES) == EXPECTED_LEGACY_NULLABLE_TENANT_OWNED_TABLES
    assert set(module.BACKFILL_SOURCES) <= set(module.TENANT_OWNED_TABLES)
    assert module.QUARANTINE_TENANT_ID == TENANT_SCOPE_QUARANTINE_ID
    assert module.QUARANTINE_TENANT_SLUG == TENANT_SCOPE_QUARANTINE_SLUG


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"


@pytest.mark.asyncio
async def test_real_upgrade_backfills_quarantines_and_separates_shared_read_write(pg_container) -> None:
    from tests.integration.conftest import APP_USER, APP_USER_PASSWORD, _async_url

    database_name = f"tenant_null_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic(database_url, "upgrade", "head")
    _alembic(database_url, "downgrade", "rls_complete_coverage_0712")

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    department_id = uuid.uuid4()
    parent_department_b_id = uuid.uuid4()
    conflicting_department_id = uuid.uuid4()
    member_id = uuid.uuid4()
    post_id = uuid.uuid4()
    tenantless_user_id = uuid.uuid4()
    tenant_a_user_id = uuid.uuid4()
    builtin_tool_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            # Empty-database bootstrap uses current metadata before stamping the
            # requested parent revision. Reopen only the two columns used to
            # inject representative legacy rows.
            await connection.execute(text("ALTER TABLE org_members ALTER COLUMN tenant_id DROP NOT NULL"))
            await connection.execute(text("ALTER TABLE plaza_posts ALTER COLUMN tenant_id DROP NOT NULL"))
            await connection.execute(text("ALTER TABLE departments ALTER COLUMN tenant_id DROP NOT NULL"))
            await connection.execute(
                text(
                    "INSERT INTO tenants "
                    "(id, name, slug, im_provider, is_active, min_heartbeat_interval_minutes, timezone, "
                    "default_max_triggers, min_poll_interval_floor, max_webhook_rate_ceiling, "
                    "tokens_used_today, tokens_used_month, tokens_used_total, sync_version) "
                    "VALUES (:id, 'Tenant A', :slug, 'web_only', true, 45, 'UTC', 20, 5, 5, 0, 0, 0, 1)"
                ),
                {"id": tenant_a, "slug": f"tenant-a-{uuid.uuid4().hex[:8]}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenants "
                    "(id, name, slug, im_provider, is_active, min_heartbeat_interval_minutes, timezone, "
                    "default_max_triggers, min_poll_interval_floor, max_webhook_rate_ceiling, "
                    "tokens_used_today, tokens_used_month, tokens_used_total, sync_version) "
                    "VALUES (:id, 'Tenant B', :slug, 'web_only', true, 45, 'UTC', 20, 5, 5, 0, 0, 0, 1)"
                ),
                {"id": tenant_b, "slug": f"tenant-b-{uuid.uuid4().hex[:8]}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO org_departments "
                    "(id, name, path, member_count, tenant_id) VALUES (:id, 'Root', '', 0, :tenant_id)"
                ),
                {"id": department_id, "tenant_id": tenant_a},
            )
            await connection.execute(
                text(
                    "INSERT INTO org_members "
                    "(id, name, title, department_id, department_path, status, tenant_id) "
                    "VALUES (:id, 'Legacy member', '', :department_id, '', 'active', NULL)"
                ),
                {"id": member_id, "department_id": department_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO plaza_posts "
                    "(id, author_id, author_type, author_name, content, tenant_id, likes_count, comments_count) "
                    "VALUES (:id, :author_id, 'agent', 'orphan', 'legacy', NULL, 0, 0)"
                ),
                {"id": post_id, "author_id": uuid.uuid4()},
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, email, password_hash, display_name, role, tenant_id, is_active, "
                    "must_change_password, tokens_used_today, tokens_used_month, tokens_used_total) "
                    "VALUES (:id, :username, :email, 'x', 'Pre-tenant', 'member', NULL, true, false, 0, 0, 0)"
                ),
                {
                    "id": tenantless_user_id,
                    "username": f"pre-{uuid.uuid4().hex[:8]}",
                    "email": f"pre-{uuid.uuid4().hex[:8]}@test.invalid",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, email, password_hash, display_name, role, tenant_id, is_active, "
                    "must_change_password, tokens_used_today, tokens_used_month, tokens_used_total) "
                    "VALUES (:id, :username, :email, 'x', 'Tenant A user', 'member', :tenant_id, "
                    "true, false, 0, 0, 0)"
                ),
                {
                    "id": tenant_a_user_id,
                    "username": f"tenant-a-{uuid.uuid4().hex[:8]}",
                    "email": f"tenant-a-{uuid.uuid4().hex[:8]}@test.invalid",
                    "tenant_id": tenant_a,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO departments (id, tenant_id, name, sort_order) "
                    "VALUES (:id, :tenant_id, 'Tenant B parent', 0)"
                ),
                {"id": parent_department_b_id, "tenant_id": tenant_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO departments (id, tenant_id, name, parent_id, manager_id, sort_order) "
                    "VALUES (:id, NULL, 'Conflicting legacy row', :parent_id, :manager_id, 0)"
                ),
                {
                    "id": conflicting_department_id,
                    "parent_id": parent_department_b_id,
                    "manager_id": tenant_a_user_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO tools "
                    "(id, name, display_name, description, type, category, icon, parameters_schema, config, "
                    "config_schema, enabled, is_default, tenant_id) "
                    "VALUES (:id, :name, 'Builtin', '', 'builtin', 'general', '', '{}'::json, '{}'::json, "
                    "'{}'::json, true, false, NULL)"
                ),
                {"id": builtin_tool_id, "name": f"builtin_{uuid.uuid4().hex[:8]}"},
            )
    finally:
        await engine.dispose()

    _alembic(database_url, "upgrade", "head")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            assert (
                await connection.scalar(text("SELECT tenant_id FROM org_members WHERE id = :id"), {"id": member_id})
                == tenant_a
            )
            quarantine_tenant = await connection.scalar(
                text("SELECT tenant_id FROM plaza_posts WHERE id = :id"), {"id": post_id}
            )
            assert str(quarantine_tenant) == "00000000-0000-4000-8000-000000000023"
            receipt = (
                await connection.execute(
                    text(
                        "SELECT source_table, source_row_id, reason FROM tenant_scope_quarantine_records "
                        "WHERE source_table = 'plaza_posts' AND source_row_id = :row_id"
                    ),
                    {"row_id": str(post_id)},
                )
            ).one()
            assert receipt.reason == "tenant authority could not be derived"
            conflict_receipt = (
                await connection.execute(
                    text(
                        "SELECT reason FROM tenant_scope_quarantine_records "
                        "WHERE source_table = 'departments' AND source_row_id = :row_id"
                    ),
                    {"row_id": str(conflicting_department_id)},
                )
            ).one()
            assert conflict_receipt.reason == "conflicting tenant authorities"
            nullable = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name = ANY(:tables) "
                    "AND column_name='tenant_id' AND is_nullable='YES'"
                ),
                {"tables": ["org_members", "plaza_posts"]},
            )
            assert nullable == 0
            tool_policy = (
                await connection.execute(text("SELECT qual, with_check FROM pg_policies WHERE tablename='tools'"))
            ).one()
            assert "type" in tool_policy.qual and "builtin" in tool_policy.qual
            assert "tenant_id IS NULL" not in tool_policy.with_check

        create_role_sql = (
            f"DO $$ BEGIN CREATE ROLE {APP_USER} LOGIN PASSWORD '{APP_USER_PASSWORD}' "
            "NOSUPERUSER NOBYPASSRLS; EXCEPTION WHEN duplicate_object THEN NULL; END $$; "
            f"GRANT CONNECT ON DATABASE {database_name} TO {APP_USER}; "
            f"GRANT USAGE ON SCHEMA public TO {APP_USER}; "
            f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {APP_USER}; "
            f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {APP_USER};"
        )
        code, output = pg_container.exec(["psql", "-U", "test", "-d", database_name, "-c", create_role_sql])
        assert code == 0, output
    finally:
        await engine.dispose()

    app_url = make_url(database_url).set(username=APP_USER, password=APP_USER_PASSWORD)
    app_engine = create_async_engine(app_url, poolclass=NullPool)
    try:
        async with app_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant, true)"),
                {"tenant": str(tenant_a)},
            )
            assert (
                await connection.scalar(text("SELECT count(*) FROM tools WHERE id = :id"), {"id": builtin_tool_id}) == 1
            )
            assert (
                await connection.scalar(text("SELECT count(*) FROM users WHERE id = :id"), {"id": tenantless_user_id})
                == 0
            )
            assert (
                await connection.scalar(text("SELECT count(*) FROM plaza_posts WHERE id = :id"), {"id": post_id}) == 0
            )
            await transaction.rollback()

        async with app_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant, true)"),
                {"tenant": str(tenant_a)},
            )
            with pytest.raises(DBAPIError, match="row-level security"):
                await connection.execute(
                    text(
                        "INSERT INTO tools "
                        "(id, name, display_name, description, type, category, icon, parameters_schema, config, "
                        "config_schema, enabled, is_default, tenant_id) "
                        "VALUES (:id, :name, 'Illegal global', '', 'builtin', 'general', '', '{}'::json, "
                        "'{}'::json, '{}'::json, true, false, NULL)"
                    ),
                    {"id": uuid.uuid4(), "name": f"illegal_{uuid.uuid4().hex[:8]}"},
                )
            await transaction.rollback()

        async with app_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("SET LOCAL app.current_tenant_id = 'BYPASS'"))
            assert (
                await connection.scalar(text("SELECT count(*) FROM plaza_posts WHERE id = :id"), {"id": post_id}) == 1
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM tenant_scope_quarantine_records WHERE source_row_id = :id"),
                    {"id": str(post_id)},
                )
                == 1
            )
            await transaction.rollback()
    finally:
        await app_engine.dispose()

    _alembic(database_url, "downgrade", "rls_complete_coverage_0712")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_name='plaza_posts' "
                        "AND column_name='tenant_id' AND is_nullable='NO'"
                    )
                )
                == 1
            )
            assert await connection.scalar(text("SELECT count(*) FROM tenant_scope_quarantine_records")) == 2
    finally:
        await engine.dispose()
