from __future__ import annotations

from pathlib import Path
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_runtime_task_root_authority_upgrade_contract(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "columns": {column["name"] for column in inspect(sync_connection).get_columns("runtime_tasks")},
                    "indexes": {item["name"] for item in inspect(sync_connection).get_indexes("runtime_tasks")},
                }
            )
        assert {"root_user_id", "root_session_id", "delegation_chain_json"} <= schema["columns"]
        assert {
            "ix_runtime_tasks_root_user_id",
            "ix_runtime_tasks_root_session_id",
            "ix_runtime_tasks_root_authority",
        } <= schema["indexes"]
    finally:
        await engine.dispose()


def test_runtime_task_root_authority_migration_backfills_all_available_truth_sources() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_task_root_authority_0711.py"
    ).read_text(encoding="utf-8")
    assert "execution_principal" in migration
    assert "runtime_budget_runs" in migration
    assert "chat_sessions" in migration
    assert "parent_session_id" in migration
    assert "delegation_chain_json" in migration
    assert "autocommit_block" in migration
    assert "LIMIT :batch_size" in migration
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert migration.count("CREATE INDEX CONCURRENTLY") == 3
    assert "server_default=sa.text(\"'[]'::jsonb\")" in migration
    assert "SELECT count(*) FROM updated" in migration
    assert ".scalar_one()" in migration
    assert ".fetchall()" not in migration


async def test_runtime_task_root_authority_batch_upgrade_uses_metadata_truth(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"runtime_authority_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255) PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO alembic_version VALUES ('channel_delivery_outbox_0711')"))
            await connection.execute(
                text(
                    """
                    CREATE TABLE runtime_budget_runs (
                        id UUID PRIMARY KEY,
                        root_user_id UUID,
                        root_session_id TEXT
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE chat_sessions (
                        id UUID PRIMARY KEY,
                        user_id UUID,
                        root_session_id UUID,
                        parent_session_id UUID
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE runtime_tasks (
                        id UUID PRIMARY KEY,
                        tenant_id UUID,
                        parent_agent_id UUID,
                        child_agent_id UUID,
                        child_agent_name TEXT,
                        task_type TEXT NOT NULL,
                        parent_session_id TEXT,
                        budget_run_id UUID,
                        metadata_json JSON
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO runtime_tasks (
                        id, tenant_id, parent_agent_id, task_type,
                        parent_session_id, metadata_json
                    ) VALUES (
                        :id, :tenant_id, :agent_id, 'workflow', :session_id,
                        json_build_object(
                            'root_user_id', CAST(:user_id AS text),
                            'root_session_id', CAST(:session_id AS text),
                            'delegation_chain', json_build_array('agent:' || CAST(:agent_id_text AS text))
                        )
                    )
                    """
                ),
                {
                    "id": task_id,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "agent_id_text": str(agent_id),
                    "user_id": str(user_id),
                    "session_id": str(uuid.uuid4()),
                },
            )
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "runtime_task_root_authority_0711")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT root_user_id, root_session_id, delegation_chain_json
                        FROM runtime_tasks WHERE id = :id
                        """
                        ),
                        {"id": task_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["root_user_id"] == user_id
        assert row["root_session_id"]
        assert row["delegation_chain_json"] == [f"agent:{agent_id}"]
    finally:
        await engine.dispose()


async def test_runtime_task_root_authority_revision_resumes_after_autocommit_checkpoint(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"runtime_authority_resume_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)

    # Current metadata represents a process killed after autocommit persisted
    # the new columns/indexes but before Alembic advanced its version receipt.
    _alembic_upgrade(database_url, "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(text("INSERT INTO alembic_version VALUES ('channel_delivery_outbox_0711')"))
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "runtime_task_root_authority_0711")

    verify_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with verify_engine.connect() as connection:
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            nullable = (
                await connection.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='runtime_tasks' "
                        "AND column_name='delegation_chain_json'"
                    )
                )
            ).scalar_one()
    finally:
        await verify_engine.dispose()

    assert version == "runtime_task_root_authority_0711"
    assert nullable == "NO"
