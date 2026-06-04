"""Real-PG fixtures for migration tests (§9 P1).

Re-exports the shared Testcontainers fixtures from tests/integration/conftest
and adds ``chain_migrated_pg_url`` — the UPGRADE-path complement to the
bootstrap-path ``migrated_pg_url``:

* ``migrated_pg_url``       — empty DB → db_bootstrap (create_all + RLS + stamp).
  This is what every fresh deployment runs.
* ``chain_migrated_pg_url`` — schema at the PREVIOUS head → ``alembic upgrade
  head`` actually EXECUTES the new workflow migration's DDL. This is what
  every existing production deployment runs on release. Without it the
  bootstrap stamp would short-circuit new migrations and their SQL would
  never be exercised by any test.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy.engine import make_url

from tests.integration.conftest import (  # noqa: F401  (re-exported fixtures)
    APP_USER,
    APP_USER_PASSWORD,
    BACKEND_ROOT,
    _async_url,
    app_user_engine,
    app_user_sessionmaker,
    migrated_pg_url,
    owner_engine,
    owner_sessionmaker,
    pg_container,
)

_PREVIOUS_HEAD = "add_mcp_server_records_0602"
_WORKFLOW_TABLES = ("workflow_definitions", "workflow_steps", "workflow_leaf_calls", "workflow_quotas")


def _alembic_upgrade_head(database_url: str) -> None:
    env = {**os.environ, "DATABASE_URL": database_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade head failed:\nstdout tail: {result.stdout[-2000:]}\nstderr tail: {result.stderr[-2000:]}"
        )


@pytest.fixture(scope="session")
def chain_migrated_pg_url(pg_container) -> str:  # noqa: F811  (pytest fixture param, not a redefinition)
    """Simulate a production upgrade so the new migration truly executes.

    Steps: (1) bootstrap a full schema in a fresh database; (2) drop the new
    workflow tables and rewind ``alembic_version`` to the previous head —
    that is byte-for-byte the state existing deployments are in; (3) run
    ``alembic upgrade head`` again, which now actually executes
    ``add_workflow_tables_0604`` (tables + indexes + ENABLE/FORCE RLS)."""
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", "CREATE DATABASE chaintest"])
    if code != 0:
        pytest.fail(f"failed to create chaintest database: {output}")

    # NB: str(URL) masks the password as '***' — must render explicitly.
    async_url = make_url(_async_url(pg_container)).set(database="chaintest").render_as_string(hide_password=False)

    # (1) full bootstrap on the empty database (create_all + RLS + stamp head)
    _alembic_upgrade_head(async_url)

    # (2) rewind to the previous head: drop the new tables, repoint the stamp
    rewind_sql = "; ".join(
        [f"DROP TABLE IF EXISTS {table} CASCADE" for table in _WORKFLOW_TABLES]
        + [f"UPDATE alembic_version SET version_num = '{_PREVIOUS_HEAD}'"]
    )
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "chaintest", "-c", rewind_sql])
    if code != 0:
        pytest.fail(f"failed to rewind chaintest to previous head: {output}")

    # (3) the real upgrade-path run — executes add_workflow_tables_0604
    _alembic_upgrade_head(async_url)
    return async_url
