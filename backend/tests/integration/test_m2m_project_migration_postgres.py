"""Project downgrade checks own their database and retain original work evidence."""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.integration import conftest as integration

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
def pg_container():
    # A rejected downgrade may commit earlier schema revisions.
    yield from integration.pg_container.__wrapped__()


@pytest.fixture()
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


async def test_project_upgrade_preserves_work_and_refuses_populated_downgrade(
    migrated_pg_url, owner_sessionmaker, owner_engine, tmp_path
):
    from app.db_bootstrap import apply_rls_policies
    from app.models.chat_session import ChatSession
    from app.models.workbench_project import WorkbenchProject
    from tests.integration.conftest import APP_USER
    from tests.services.test_session_input_control_v2 import _seed_session

    tenant, user, _agent, session, _run = await _seed_session(owner_sessionmaker)

    def migrate(action, target):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "alembic", action, target],
            env={**os.environ, "DATABASE_URL": migrated_pg_url},
            capture_output=True,
            text=True,
            timeout=60,
        )
        (tmp_path / f"{action}-{target}.log").write_text(result.stdout + result.stderr)
        return result

    empty = await asyncio.to_thread(migrate, "downgrade", "private_environment_1018")
    assert empty.returncode == 0, empty.stderr
    upgraded = await asyncio.to_thread(migrate, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    project = uuid4()
    async with owner_sessionmaker() as db:
        assert (await db.get(ChatSession, session)).user_id == user
        db.add(WorkbenchProject(id=project, tenant_id=tenant, owner_user_id=user, title="Retained project", revision=1))
        await db.commit()
    refused = await asyncio.to_thread(migrate, "downgrade", "private_environment_1018")
    assert refused.returncode != 0 and "Retain project organization and references" in refused.stderr
    async with owner_sessionmaker() as db:
        assert (await db.get(WorkbenchProject, project)).title == "Retained project"
        assert (await db.get(ChatSession, session)).user_id == user
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "workbench_projects_1019"
    tables = ("workbench_projects", "workbench_project_entries")
    async with owner_engine.begin() as connection:
        for table in tables:
            await connection.execute(text(f"DROP POLICY tenant_isolation ON {table}"))
        await connection.run_sync(lambda sync: apply_rls_policies(sync, tables=tables, forced_tables=tables))
        rows = (
            await connection.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecuritY FROM pg_class WHERE relname IN ('workbench_projects','workbench_project_entries')"
                )
            )
        ).all()
        assert len(rows) == 2 and all(row == (True, True) for row in rows)
        await connection.execute(text(f"GRANT ALL ON workbench_projects,workbench_project_entries TO {APP_USER}"))
