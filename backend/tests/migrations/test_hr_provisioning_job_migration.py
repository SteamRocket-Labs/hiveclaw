from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "hr_provisioning_jobs_0712.py"


def _alembic_downgrade(database_url: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"


def test_hr_provisioning_job_migration_has_durable_backfill_and_rollback_contract() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "hr_provisioning_jobs_0712"' in source
    assert 'down_revision = "approval_execution_jobs_0712"' in source
    assert "provisioning_task_id" in source
    assert "task_type = 'hr_provisioning'" in source
    assert "status IN ('confirmed','creating','provisioning','failed')" in source
    assert "ON CONFLICT (root_idempotency_key) DO NOTHING" in source
    assert "DISABLE ROW LEVEL SECURITY" in source
    assert "DELETE FROM runtime_tasks WHERE task_type = 'hr_provisioning'" in source


async def test_hr_provisioning_job_migration_really_backfills_confirmed_draft(pg_container) -> None:
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import (
        _alembic_upgrade,
        insert_agent_at_schema_revision,
        insert_chat_session_at_schema_revision,
    )

    database_name = f"hrjob_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic_upgrade(database_url, "head")
    _alembic_downgrade(database_url, "approval_execution_jobs_0712")

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    draft_id = uuid.uuid4()
    blueprint = {"name": "Researcher"}
    encoded_blueprint = json.dumps(blueprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    blueprint_hash = f"bp_{hashlib.sha256(encoded_blueprint).hexdigest()[:24]}"
    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            db.add(Tenant(id=tenant_id, name="HR Job", slug=f"hr-job-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"hr-job-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@hr-job.test",
                    password_hash="x",
                    display_name="HR Owner",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            await insert_agent_at_schema_revision(
                db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                name="__system_hr__",
                creator_id=user_id,
                agent_class="internal_system",
                status="running",
            )
            await insert_chat_session_at_schema_revision(
                db,
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            await db.execute(
                text(
                    "INSERT INTO hr_creation_drafts ("
                    "id, tenant_id, hr_agent_id, session_id, requested_by_user_id, status, "
                    "blueprint_version, blueprint_hash, blueprint_json, preview_json, "
                    "confirmed_by_user_id, confirmed_at, claim_version, attempt_count, provisioning_json"
                    ") VALUES ("
                    ":id, :tenant_id, :agent_id, :session_id, :user_id, 'confirmed', "
                    "3, :blueprint_hash, CAST(:blueprint AS jsonb), CAST(:preview AS jsonb), "
                    ":user_id, :confirmed_at, 0, 0, '{}'::jsonb)"
                ),
                {
                    "id": draft_id,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "blueprint_hash": blueprint_hash,
                    "blueprint": json.dumps(blueprint, separators=(",", ":")),
                    "preview": '{"status":"preview"}',
                    "confirmed_at": datetime.now(timezone.utc),
                },
            )
            await db.commit()
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT draft.provisioning_task_id, task.task_type, task.status, "
                        "task.root_idempotency_key, draft.provisioning_json "
                        "FROM hr_creation_drafts AS draft "
                        "JOIN runtime_tasks AS task ON task.id = draft.provisioning_task_id "
                        "WHERE draft.id = :draft_id"
                    ),
                    {"draft_id": draft_id},
                )
            ).one()
        assert row.task_type == "hr_provisioning"
        assert row.status == "pending"
        assert row.root_idempotency_key == f"hr-provisioning:{draft_id}-v3"
        assert row.provisioning_json["runtime_task_id"] == str(row.provisioning_task_id)
    finally:
        await engine.dispose()


async def test_hr_provisioning_job_adopts_startup_created_draft_column(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"hrjob_startup_drift_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)

    # metadata.create_all created the latest empty HR table while the mechanical
    # Alembic receipt was still on the preceding revision.
    _alembic_upgrade(database_url, "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(text("INSERT INTO alembic_version VALUES ('approval_execution_jobs_0712')"))
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "hr_provisioning_jobs_0712")

    verify_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with verify_engine.connect() as connection:
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    finally:
        await verify_engine.dispose()

    assert version == "hr_provisioning_jobs_0712"
