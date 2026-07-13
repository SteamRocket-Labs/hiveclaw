from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import BACKEND_ROOT, _async_url


def _run_alembic(database_url: str, target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )


async def test_approval_execution_envelope_upgrade_adds_bound_columns(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"] for column in inspect(sync_connection).get_columns("approval_requests")
                }
            )
        assert {"execution_envelope", "execution_envelope_hash"} <= columns
    finally:
        await engine.dispose()


def test_approval_execution_envelope_migration_invalidates_legacy_tool_tickets_reversibly() -> None:
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "approval_execution_envelope_0711.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.down_revision == "plan_authorization_lease_0711"
    assert migration.LEGACY_INVALIDATION_MARKER == "approval_execution_envelope_0711"
    source = path.read_text(encoding="utf-8")
    assert "execution_status = 'needs_reapproval'" in source
    assert "legacy_approval_previous_status" in source
    assert "legacy_approval_previous_execution_status" in source
    assert "legacy_approval_previous_resolved_at" in source
    assert "execution_envelope IS NULL" in source
    assert "legacy_approval_invalidated_by" in source
    assert "DROP COLUMN execution_envelope" not in source
    assert 'op.drop_column("approval_requests", "execution_envelope")' in source


@pytest.mark.asyncio
async def test_approval_execution_envelope_upgrades_legacy_json_details(pg_container) -> None:
    database_name = f"approval_json_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    bootstrap = _run_alembic(database_url, "head")
    assert bootstrap.returncode == 0, bootstrap.stderr[-4000:]

    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory.begin() as session:
            session.add(Tenant(id=tenant_id, name="Approval JSON", slug=f"approval-{tenant_id.hex[:8]}"))
            session.add(
                User(
                    id=user_id,
                    username=f"approval-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@approval.test",
                    password_hash="x",
                    display_name="Owner",
                    tenant_id=tenant_id,
                    role="org_admin",
                )
            )
            await session.flush()
            session.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name="Approval Agent",
                    creator_id=user_id,
                    status="idle",
                )
            )
            await session.flush()
            session.add(
                ApprovalRequest(
                    id=approval_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    action_type="tool_execution",
                    details={"source": "legacy-json"},
                    status="pending",
                    requested_by=user_id,
                    tool_name="write_file",
                    execution_status="pending",
                )
            )

        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE approval_requests DROP COLUMN execution_envelope_hash"))
            await connection.execute(text("ALTER TABLE approval_requests DROP COLUMN execution_envelope"))
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('plan_authorization_lease_0711')")
            )
    finally:
        await engine.dispose()

    upgrade = _run_alembic(database_url, "approval_execution_envelope_0711")
    assert upgrade.returncode == 0, f"stdout tail: {upgrade.stdout[-4000:]}\nstderr tail: {upgrade.stderr[-4000:]}"

    verify_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with verify_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status::text, execution_status, "
                        "details::jsonb->>'source', "
                        "details::jsonb->>'legacy_approval_invalidated_by' "
                        "FROM approval_requests WHERE id=:approval_id"
                    ),
                    {"approval_id": approval_id},
                )
            ).one()
            envelope_type = (
                await connection.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='approval_requests' "
                        "AND column_name='execution_envelope'"
                    )
                )
            ).scalar_one()
    finally:
        await verify_engine.dispose()

    assert row == (
        "rejected",
        "needs_reapproval",
        "legacy-json",
        "approval_execution_envelope_0711",
    )
    assert envelope_type == "json"
