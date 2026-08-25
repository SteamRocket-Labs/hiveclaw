from __future__ import annotations

from pathlib import Path
import uuid

from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _schema_snapshot(database_url: str) -> dict:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            def snapshot(sync_connection):
                inspector = inspect(sync_connection)
                tables = set(inspector.get_table_names())
                return {
                    "tables": tables,
                    "runtime_columns": {
                        column["name"] for column in inspector.get_columns("runtime_tasks")
                    },
                    "environment_indexes": (
                        {item["name"] for item in inspector.get_indexes("execution_environments")}
                        if "execution_environments" in tables
                        else set()
                    ),
                }

            return await connection.run_sync(snapshot)
    finally:
        await engine.dispose()


async def test_agent_environment_schema_and_runtime_links_exist(revision_parent_migrated_pg_url: str) -> None:
    schema = await _schema_snapshot(revision_parent_migrated_pg_url)

    assert {
        "execution_environments",
        "environment_sessions",
        "environment_leases",
        "environment_checkpoints",
    } <= schema["tables"]
    assert {
        "environment_id",
        "environment_session_id",
        "environment_lease_id",
        "environment_checkpoint_id",
    } <= schema["runtime_columns"]
    assert "uq_execution_environments_agent_private" in schema["environment_indexes"]


async def test_agent_environment_revision_round_trips_on_real_postgres(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    database_name = f"agent_environment_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(
        ["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"]
    )
    assert code == 0, output
    database_url = (
        make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    )

    _alembic_upgrade(database_url, "head")
    upgraded = await _schema_snapshot(database_url)
    assert "execution_environments" in upgraded["tables"]

    _alembic_downgrade(database_url, "merge_incident_kimi_0725")
    downgraded = await _schema_snapshot(database_url)
    assert "execution_environments" not in downgraded["tables"]
    assert "environment_id" not in downgraded["runtime_columns"]

    _alembic_upgrade(database_url, "head")
    reupgraded = await _schema_snapshot(database_url)
    assert "execution_environments" in reupgraded["tables"]
    assert "environment_id" in reupgraded["runtime_columns"]


def test_agent_environment_migration_has_strict_rls_and_no_legacy_backfill() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "202608250900_agent_environment_control_plane.py"
    ).read_text(encoding="utf-8")

    assert migration.count("ENABLE ROW LEVEL SECURITY") >= 1
    assert migration.count("FORCE ROW LEVEL SECURITY") >= 1
    assert "current_setting('app.current_tenant_id', true)" in migration
    assert "enforce_environment_tenant_binding" in migration
    assert "enforce_runtime_task_environment_binding" in migration
    assert "INSERT INTO execution_environments" not in migration
