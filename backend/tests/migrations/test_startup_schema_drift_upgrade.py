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

from tests.integration.conftest import BACKEND_ROOT, _async_url


def _run_alembic(
    database_url: str,
    command: str,
    target: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.mark.asyncio
async def test_versioned_database_with_startup_create_all_can_apply_blocked_revision(pg_container) -> None:
    """Reproduce the production drift caused by startup create_all before Alembic.

    The database first reaches the last pre-release revision through the real
    migration chain.  Current model metadata is then applied exactly as the
    runtime startup used to do: existing tables are left untouched while new
    release tables are created early.  The first blocked Alembic revision must
    reconcile its empty bootstrap table and still execute its backfills, policy,
    trigger, and version advance.
    """

    database_name = f"startup_drift_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)

    # Bootstrap current metadata, then reconstruct the first blocked revision's
    # preconditions.  Repository history predates Alembic and therefore cannot
    # be replayed from an empty schema; this focused setup exercises the exact
    # production collision without relying on incomplete legacy downgrades.
    bootstrap = _run_alembic(database_url, "upgrade", "head")
    assert bootstrap.returncode == 0, bootstrap.stderr[-4000:]

    from app.database import Base
    from app.models import import_all_models

    import_all_models()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text("DROP TRIGGER IF EXISTS trg_config_revision_immutability ON config_revisions")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS enforce_config_revision_immutability()"))
            await connection.execute(text("DROP INDEX IF EXISTS ix_config_revisions_parent_revision_id"))
            await connection.execute(text("DROP INDEX IF EXISTS ix_config_revisions_rollback_of_revision_id"))
            await connection.execute(text("ALTER TABLE config_revisions DROP COLUMN IF EXISTS parent_revision_id"))
            await connection.execute(text("ALTER TABLE config_revisions DROP COLUMN IF EXISTS rollback_of_revision_id"))
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": "runtime_assembly_nested_0710"},
            )
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            early_table_count = (await connection.execute(text("SELECT count(*) FROM ai_asset_records"))).scalar_one()
    finally:
        await engine.dispose()

    assert version == "runtime_assembly_nested_0710"
    assert early_table_count == 0

    upgrade = _run_alembic(database_url, "upgrade", "ai_asset_control_plane_0710")
    assert upgrade.returncode == 0, f"stdout tail: {upgrade.stdout[-4000:]}\nstderr tail: {upgrade.stderr[-4000:]}"

    verify_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with verify_engine.connect() as connection:
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            trigger_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgname = 'trg_config_revision_immutability' AND NOT tgisinternal"
                    )
                )
            ).scalar_one()
    finally:
        await verify_engine.dispose()

    assert version == "ai_asset_control_plane_0710"
    assert trigger_count == 1
