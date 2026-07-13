import asyncio
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import enter_rls_bypass
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.tenant import Tenant
from app.models.user import User


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "system_plan_notification_outbox_0713.py"
)


def test_system_plan_notification_outbox_migration_contract() -> None:
    assert MIGRATION_PATH.exists()
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "system_plan_outbox_0713"' in source
    assert 'down_revision = "workflow_completion_outbox_0713"' in source
    assert "ck_runtime_notification_outbox_source_kind" in source
    assert "system_plan_run" in source
    assert "DISABLE ROW LEVEL SECURITY" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source


async def test_system_plan_notification_outbox_upgrade_installs_real_postgres_constraint(
    chain_migrated_pg_url: str,
) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            checks = await connection.run_sync(
                lambda sync_connection: {
                    constraint["name"]: constraint["sqltext"]
                    for constraint in inspect(sync_connection).get_check_constraints("runtime_notification_outbox")
                }
            )
        assert "system_plan_run" in checks["ck_runtime_notification_outbox_source_kind"]
    finally:
        await engine.dispose()


def _run_alembic(database_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    backend_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=backend_root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )


async def test_system_plan_notification_outbox_downgrade_roundtrip_under_forced_rls_owner(
    chain_migrated_pg_url: str,
) -> None:
    """The ordinary table owner remains subject to FORCE RLS during downgrade."""

    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    outbox_id = uuid4()
    source_run_id = str(uuid4())
    role_name = f"system_plan_migration_owner_{uuid4().hex[:12]}"
    role_password = f"pw_{uuid4().hex}"
    original_owners: dict[str, str] = {}
    original_rls_state: tuple[bool, bool] | None = None

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="test seed System Plan migration downgrade row",
        ) as bypass_db,
    ):
        bypass_db.add(Tenant(id=tenant_id, name="Migration tenant", slug=f"migration-{tenant_id.hex[:10]}"))
        bypass_db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"migration-{user_id.hex[:10]}",
                email=f"migration-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Migration owner",
            )
        )
        await bypass_db.flush()
        bypass_db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Migration agent",
                role_description="migration test",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await bypass_db.flush()
        bypass_db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Migration session",
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        bypass_db.add(
            RuntimeNotificationOutbox(
                id=outbox_id,
                tenant_id=tenant_id,
                source_kind="system_plan_run",
                source_run_id=source_run_id,
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="resumable",
                task_type="system_plan_run",
                summary="System Plan retry scheduled",
                delivery_mode="session_projection",
            )
        )
        await bypass_db.commit()

    async with engine.begin() as connection:
        rls_row = (
            await connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = 'runtime_notification_outbox'::regclass"
                )
            )
        ).one()
        original_rls_state = (bool(rls_row.relrowsecurity), bool(rls_row.relforcerowsecurity))
        for table_name in (
            "runtime_notification_outbox",
            "runtime_tasks",
            "alembic_version",
        ):
            original_owners[table_name] = str(
                await connection.scalar(
                    text("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = CAST(:table_name AS regclass)"),
                    {"table_name": table_name},
                )
            )
        await connection.execute(text(f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{role_password}'"))
        await connection.execute(text(f'GRANT USAGE, CREATE ON SCHEMA public TO "{role_name}"'))
        await connection.execute(text(f'ALTER TABLE runtime_notification_outbox OWNER TO "{role_name}"'))
        await connection.execute(text(f'ALTER TABLE runtime_tasks OWNER TO "{role_name}"'))
        await connection.execute(text(f'ALTER TABLE alembic_version OWNER TO "{role_name}"'))

    role_url = (
        make_url(chain_migrated_pg_url)
        .set(username=role_name, password=role_password)
        .render_as_string(hide_password=False)
    )
    try:
        downgrade = await asyncio.to_thread(
            _run_alembic,
            role_url,
            "downgrade",
            "workflow_completion_outbox_0713",
        )
        assert downgrade.returncode == 0, downgrade.stdout[-2000:] + downgrade.stderr[-2000:]
        async with engine.connect() as connection:
            remaining = await connection.scalar(
                text(
                    "SELECT count(*) FROM runtime_notification_outbox "
                    "WHERE source_kind = 'system_plan_run' AND source_run_id = :source_run_id"
                ),
                {"source_run_id": source_run_id},
            )
            legacy_check = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'runtime_notification_outbox'::regclass "
                    "AND conname = 'ck_runtime_notification_outbox_source_kind'"
                )
            )
            restored_rls_row = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE oid = 'runtime_notification_outbox'::regclass"
                    )
                )
            ).one()
        assert remaining == 0
        assert "system_plan_run" not in str(legacy_check)
        assert (bool(restored_rls_row.relrowsecurity), bool(restored_rls_row.relforcerowsecurity)) == original_rls_state

        upgrade = await asyncio.to_thread(_run_alembic, role_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stdout[-2000:] + upgrade.stderr[-2000:]
        async with engine.connect() as connection:
            upgraded_check = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'runtime_notification_outbox'::regclass "
                    "AND conname = 'ck_runtime_notification_outbox_source_kind'"
                )
            )
        assert "system_plan_run" in str(upgraded_check)
    finally:
        await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'ALTER TABLE runtime_notification_outbox OWNER TO "{original_owners["runtime_notification_outbox"]}"'
                )
            )
            await connection.execute(text(f'ALTER TABLE runtime_tasks OWNER TO "{original_owners["runtime_tasks"]}"'))
            await connection.execute(
                text(f'ALTER TABLE alembic_version OWNER TO "{original_owners["alembic_version"]}"')
            )
            await connection.execute(
                text("DELETE FROM runtime_notification_outbox WHERE id = :outbox_id"),
                {"outbox_id": outbox_id},
            )
            await connection.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await connection.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            await connection.execute(text(f'REVOKE USAGE, CREATE ON SCHEMA public FROM "{role_name}"'))
            await connection.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))
        await engine.dispose()
