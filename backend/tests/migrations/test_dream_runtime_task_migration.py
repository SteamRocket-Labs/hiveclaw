from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "dream_runtime_task_0712.py"


def test_dream_runtime_task_migration_extends_and_rolls_back_task_type_contract() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "dream_runtime_task_0712"' in source
    assert 'down_revision = "hr_provisioning_jobs_0712"' in source
    assert '"dream"' in source
    assert "ck_runtime_tasks_task_type" in source
    assert "DISABLE ROW LEVEL SECURITY" in source


async def test_dream_runtime_task_upgrade_installs_real_postgres_constraint(revision_parent_migrated_pg_url: str) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            checks = await connection.run_sync(
                lambda sync_connection: {
                    constraint["name"]: constraint["sqltext"]
                    for constraint in inspect(sync_connection).get_check_constraints("runtime_tasks")
                }
            )
        assert "dream" in checks["ck_runtime_tasks_task_type"]
    finally:
        await engine.dispose()
