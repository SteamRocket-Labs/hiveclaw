"""Real-PG fixtures for migration tests (§9 P1).

Re-exports the shared Testcontainers fixtures from tests/integration/conftest
and adds ``chain_migrated_pg_url`` — the UPGRADE-path complement to the
bootstrap-path ``migrated_pg_url``:

* ``migrated_pg_url``       — empty DB → db_bootstrap (create_all + RLS + stamp).
  This is what every fresh deployment runs.
* ``chain_migrated_pg_url`` — empty DB → ``alembic upgrade <head's parent>`` →
  ``alembic upgrade head``. The second stage truly EXECUTES the newest
  migration's DDL, which is what every existing production deployment runs on
  release. Without it the bootstrap stamp would short-circuit new migrations
  and their SQL would never be exercised by any test.

The two-stage design is deliberate: the previous implementation bootstrapped
to head and then *rewound* by dropping a hand-maintained list of
latest-migration tables/columns/indexes. Every new migration silently rotted
that list (unlisted new tables collided with ``CREATE TABLE`` on the replay —
the DuplicateTableError class of errors). Upgrading the empty database through
the chain itself needs no list and cannot rot: the head's parent is derived
from the alembic script directory at runtime.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any
import uuid

import pytest
from sqlalchemy import MetaData, Table
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


def _current_head_parent() -> str:
    """The down_revision of the single current head — the release-upgrade start point."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        pytest.fail("alembic script directory has no current head")
    down_revision = script.get_revision(head).down_revision
    if not isinstance(down_revision, str) or not down_revision:
        pytest.fail(f"current head {head!r} has no single down_revision: {down_revision!r}")
    return down_revision


def _alembic_upgrade(database_url: str, target: str) -> None:
    env = {**os.environ, "DATABASE_URL": database_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade {target} failed:\n"
            f"stdout tail: {result.stdout[-2000:]}\nstderr tail: {result.stderr[-2000:]}"
        )


async def insert_agent_at_schema_revision(
    db: Any,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    creator_id: uuid.UUID,
    name: str,
    **overrides: Any,
) -> None:
    """Seed an Agent against a historical schema without current-head columns.

    Migration tests intentionally downgrade the database while importing the
    current ORM. Reflecting the live table keeps those fixtures honest as new
    Agent columns are added at later revisions.
    """
    from app.models.agent import Agent
    from app.models.participant import Participant

    participant_id = overrides.pop("participant_id", uuid.uuid4())
    sponsor_user_id = overrides.pop("sponsor_user_id", creator_id)
    db.add(
        Participant(
            id=participant_id,
            type="agent",
            ref_id=agent_id,
            display_name=name[:100],
        )
    )
    await db.flush()

    connection = await db.connection()

    def reflect_agents(sync_connection):
        return Table("agents", MetaData(), autoload_with=sync_connection)

    historical_agents = await connection.run_sync(reflect_agents)
    values: dict[str, Any] = {
        "id": agent_id,
        "tenant_id": tenant_id,
        "creator_id": creator_id,
        "sponsor_user_id": sponsor_user_id,
        "participant_id": participant_id,
        "name": name,
        **overrides,
    }
    for column in Agent.__table__.columns:
        if column.name in values or column.name not in historical_agents.c:
            continue
        if column.default is not None and column.default.is_scalar:
            values[column.name] = column.default.arg
    await db.execute(historical_agents.insert().values(**values))


@pytest.fixture(scope="session")
def chain_migrated_pg_url(pg_container) -> str:  # noqa: F811  (pytest fixture param, not a redefinition)
    """Simulate a production release upgrade so the newest migration truly executes.

    Steps: (1) create a fresh database; (2) ``alembic upgrade`` to the current
    head's PARENT — the state an existing deployment is in before the release;
    (3) ``alembic upgrade head`` — actually executes the newest migration's DDL.
    """
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", "CREATE DATABASE chaintest"])
    if code != 0:
        pytest.fail(f"failed to create chaintest database: {output}")

    # NB: str(URL) masks the password as '***' — must render explicitly.
    async_url = make_url(_async_url(pg_container)).set(database="chaintest").render_as_string(hide_password=False)

    _alembic_upgrade(async_url, _current_head_parent())
    _alembic_upgrade(async_url, "head")
    return async_url
