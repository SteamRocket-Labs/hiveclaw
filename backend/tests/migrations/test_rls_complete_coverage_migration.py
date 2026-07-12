from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "rls_complete_coverage_0712.py"
EXPECTED_TABLES = {
    "agent_teams",
    "agent_team_members",
    "agent_team_events",
    "agent_collaboration_groups",
    "agent_collaboration_group_members",
    "agent_session_goals",
    "ai_asset_usage_events",
    "local_agent_channels",
    "local_agent_channel_events",
    "local_agent_channel_messages",
    "local_agent_channel_sessions",
    "local_agent_channel_ws_tickets",
    "workspace_resource_manifests",
}


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
    assert result.returncode == 0, f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"


def test_rls_complete_coverage_migration_contract() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "rls_complete_coverage_0712"' in source
    assert 'down_revision = "mcp_metadata_trust_0712"' in source
    for table in EXPECTED_TABLES:
        assert f'"{table}"' in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "WITH CHECK" in source
    assert "secure downgrade" in source.lower()
    assert "DISABLE ROW LEVEL SECURITY" not in source


@pytest.mark.asyncio
async def test_real_upgrade_repairs_all_missing_policies_and_secure_downgrade_keeps_them(pg_container) -> None:
    from tests.integration.conftest import _async_url

    database_name = f"rls_complete_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic(database_url, "upgrade", "head")
    _alembic(database_url, "downgrade", "mcp_metadata_trust_0712")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for table in EXPECTED_TABLES:
                await connection.execute(text(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}"))
                await connection.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
                await connection.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    finally:
        await engine.dispose()

    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            flags = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:tables)"
                    ),
                    {"tables": sorted(EXPECTED_TABLES)},
                )
            ).all()
            policies = (
                await connection.execute(
                    text(
                        "SELECT tablename, policyname, qual, with_check FROM pg_policies WHERE tablename = ANY(:tables)"
                    ),
                    {"tables": sorted(EXPECTED_TABLES)},
                )
            ).all()
    finally:
        await engine.dispose()

    assert {row.relname for row in flags} == EXPECTED_TABLES
    assert all(row.relrowsecurity and row.relforcerowsecurity for row in flags)
    assert {row.tablename for row in policies} == EXPECTED_TABLES
    assert all(row.policyname == f"tenant_isolation_{row.tablename}" for row in policies)
    assert all(row.qual and row.with_check for row in policies)

    _alembic(database_url, "downgrade", "mcp_metadata_trust_0712")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            protected_after_downgrade = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class WHERE relname = ANY(:tables) "
                    "AND relrowsecurity AND relforcerowsecurity"
                ),
                {"tables": sorted(EXPECTED_TABLES)},
            )
    finally:
        await engine.dispose()

    assert protected_after_downgrade == len(EXPECTED_TABLES)
