"""Browser downgrade checks own a database and retain every resource receipt."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.models.environment_browser import EnvironmentBrowser
from tests.integration import conftest as integration
from tests.integration.conftest import APP_USER
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_control_postgres import call


@pytest.fixture()
def pg_container():
    # A rejected multi-revision downgrade can commit earlier revisions.
    yield from integration.pg_container.__wrapped__()


@pytest.fixture()
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


async def test_browser_migration_upgrades_and_refuses_a_lossy_downgrade(
    browser_case, migrated_pg_url, owner_engine, owner_sessionmaker
):
    _, source, manager, _, root = browser_case
    expected_head = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic")).get_current_head()
    assert expected_head is not None

    async def migrate(action, target):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-B", "-m", "alembic", action, target],
            env={**os.environ, "DATABASE_URL": migrated_pg_url},
            capture_output=True,
            text=True,
            timeout=60,
        )
        (root / f"migration-{action}-{target}.log").write_text(result.stdout + result.stderr)
        return result

    downgraded = await migrate("downgrade", "delegation_permissions_1022")
    assert downgraded.returncode == 0, downgraded.stderr
    upgraded = await migrate("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    async with owner_sessionmaker() as db:
        assert (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == expected_head
    async with owner_engine.begin() as conn:
        await conn.execute(text(f"GRANT ALL ON environment_browsers TO {APP_USER}"))
        assert (
            await conn.execute(
                text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='environment_browsers'")
            )
        ).one() == (True, True)
    view = (await call(manager, source, "start"))["browser"]
    refused = await migrate("downgrade", "delegation_permissions_1022")
    assert refused.returncode != 0 and "Retain private browser resource and control evidence" in refused.stderr
    async with owner_sessionmaker() as db:
        assert await db.get(EnvironmentBrowser, UUID(view["id"])) is not None
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "private_browser_control_1023"
    repaired = await migrate("upgrade", "head")
    assert repaired.returncode == 0, repaired.stderr
    async with owner_sessionmaker() as db:
        assert (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == expected_head
