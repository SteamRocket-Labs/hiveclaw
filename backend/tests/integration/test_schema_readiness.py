from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"


@pytest.mark.asyncio
async def test_real_schema_readiness_detects_force_drift_and_survives_rollback_reupgrade(pg_container) -> None:
    from app.scripts.verify_schema_readiness import inspect_schema_readiness
    from tests.integration.conftest import _async_url

    database_name = f"schema_ready_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic(database_url, "upgrade", "head")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            initial = await inspect_schema_readiness(connection)
        assert initial.ready is True
        assert initial.checked_trigger_count == 4

        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE runtime_tasks NO FORCE ROW LEVEL SECURITY"))
        async with engine.connect() as connection:
            drifted = await inspect_schema_readiness(connection)
        assert "rls_not_forced" in {issue.code for issue in drifted.issues if issue.object_name == "runtime_tasks"}

        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY"))
        async with engine.connect() as connection:
            assert (await inspect_schema_readiness(connection)).ready is True
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", "session_permission_semantics_0713")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rolled_back = await inspect_schema_readiness(connection)
        assert rolled_back.ready is False
        rollback_issue_codes = {issue.code for issue in rolled_back.issues}
        assert "alembic_head_mismatch" in rollback_issue_codes
        assert "schema_trigger_missing" in rollback_issue_codes
    finally:
        await engine.dispose()

    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            restored = await inspect_schema_readiness(connection)
        assert restored.ready is True
        assert restored.checked_trigger_count == 4
    finally:
        await engine.dispose()
