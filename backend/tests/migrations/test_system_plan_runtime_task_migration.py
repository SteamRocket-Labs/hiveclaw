from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "system_plan_runtime_task_0713.py"


def test_system_plan_runtime_task_migration_contract() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "system_plan_runtime_task_0713"' in source
    assert 'down_revision = "hr_draft_recovery_0712"' in source
    assert '"system_plan_run"' in source
    assert "ck_runtime_tasks_task_type" in source
    assert "DISABLE ROW LEVEL SECURITY" in source
    assert "DELETE FROM runtime_tasks WHERE task_type = 'system_plan_run'" in source


async def test_system_plan_runtime_task_upgrade_installs_real_postgres_constraint(
    chain_migrated_pg_url: str,
) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            checks = await connection.run_sync(
                lambda sync_connection: {
                    constraint["name"]: constraint["sqltext"]
                    for constraint in inspect(sync_connection).get_check_constraints("runtime_tasks")
                }
            )
        assert "system_plan_run" in checks["ck_runtime_tasks_task_type"]
    finally:
        await engine.dispose()
