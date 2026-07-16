"""Real-PG fixtures for migration tests (§9 P1).

Re-exports the shared Testcontainers fixtures from tests/integration/conftest
and adds ``revision_parent_migrated_pg_url`` — an isolated parent→head proof
for the newest revision, complementary to bootstrap-path ``migrated_pg_url``:

* ``migrated_pg_url``       — empty DB → db_bootstrap (create_all + RLS + stamp).
  This is what every fresh deployment runs.
* ``revision_parent_migrated_pg_url`` — production bootstrap of current
  metadata → exact subtraction of only the newest revision's owned objects →
  stamp that revision's parent → ordinary ``alembic upgrade head``. This is a
  revision-isolated migration proof, not a claim that the historical chain was
  replayed from revision zero. Tests prove every object owned by the newest
  revision is absent before Alembic executes it.
"""

from __future__ import annotations

import os
import importlib.util
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


def _bootstrap_current_head(database_url: str) -> None:
    """Run the production fresh-database bootstrap before projecting parent."""

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
            "fresh bootstrap to head failed:\n"
            f"stdout tail: {result.stdout[-2000:]}\nstderr tail: {result.stderr[-2000:]}"
        )


def _session_v2_revision_module():
    migration_path = BACKEND_ROOT / "alembic" / "versions" / "session_v2_0716.py"
    spec = importlib.util.spec_from_file_location("session_v2_parent_projection", migration_path)
    if spec is None or spec.loader is None:
        pytest.fail(f"cannot load Session V2 migration: {migration_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project_head_to_session_v2_parent(container, *, database: str) -> None:
    """Remove exactly the artifacts owned by ``session_v2_0716`` and stamp parent."""

    revision = _session_v2_revision_module()
    statements = [
        # Post-Session-V2 Group 3 head artifact. The release fixture projects
        # back through Session V2 and must not leave the newest table behind
        # before the ordinary upgrade replays runtime_root_ledger_0716.
        "DROP TABLE IF EXISTS runtime_root_items CASCADE",
        "DROP TRIGGER IF EXISTS trg_session_writer_epoch ON runtime_tasks",
        "DROP TRIGGER IF EXISTS trg_session_event_v2_contract ON chat_transcript_events",
    ]
    statements.extend(
        f'ALTER TABLE chat_transcript_events DROP COLUMN IF EXISTS "{column}" CASCADE'
        for column in revision.SESSION_V2_EVENT_COLUMNS
    )
    statements.extend(
        f'ALTER TABLE runtime_tasks DROP COLUMN IF EXISTS "{column}" CASCADE'
        for column in revision.SESSION_V2_RUNTIME_TASK_COLUMNS
    )
    statements.extend(
        f'DROP TABLE IF EXISTS "{table}" CASCADE'
        for table in (*revision.SESSION_V2_TENANT_TABLES, *revision.SESSION_V2_GLOBAL_TABLES)
    )
    statements.extend(
        f"DROP FUNCTION IF EXISTS {function_name} CASCADE" for function_name in revision.SESSION_V2_FUNCTIONS
    )
    statements.extend(
        [
            "DELETE FROM alembic_version",
            f"INSERT INTO alembic_version(version_num) VALUES ('{revision.down_revision}')",
        ]
    )
    sql = ";\n".join(statements) + ";"
    code, output = container.exec(["psql", "-v", "ON_ERROR_STOP=1", "-U", "test", "-d", database, "-c", sql])
    if code != 0:
        pytest.fail(f"failed to project Session V2 parent schema: {output}")


def _assert_session_v2_parent_projection(container, *, database: str) -> None:
    revision = _session_v2_revision_module()
    table_names = (*revision.SESSION_V2_TENANT_TABLES, *revision.SESSION_V2_GLOBAL_TABLES)
    table_array = ",".join(f"'{name}'" for name in table_names)
    event_columns = ",".join(f"'{name}'" for name in revision.SESSION_V2_EVENT_COLUMNS)
    runtime_columns = ",".join(f"'{name}'" for name in revision.SESSION_V2_RUNTIME_TASK_COLUMNS)
    query = f"""
      SELECT
        (SELECT version_num FROM alembic_version),
        (SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename IN ({table_array})),
        (SELECT count(*) FROM information_schema.columns
          WHERE table_schema='public' AND table_name='chat_transcript_events'
            AND column_name IN ({event_columns})),
        (SELECT count(*) FROM information_schema.columns
          WHERE table_schema='public' AND table_name='runtime_tasks'
            AND column_name IN ({runtime_columns})),
        (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
          AND tgname IN ('trg_session_event_v2_contract','trg_session_writer_epoch')),
        (SELECT count(*) FROM pg_proc
          WHERE proname IN ('enforce_session_event_v2_contract','enforce_session_writer_epoch','enforce_session_v2_tenant_binding'))
    """
    code, output = container.exec(["psql", "-U", "test", "-d", database, "-At", "-F", "|", "-c", query])
    if code != 0:
        pytest.fail(f"failed to inspect projected parent schema: {output}")
    evidence = (output.decode() if isinstance(output, bytes) else str(output)).strip()
    expected = f"{revision.down_revision}|0|0|0|0|0"
    if evidence != expected:
        pytest.fail(f"Session V2 parent projection is not exact: expected {expected!r}, got {evidence!r}")


def _prepare_session_v2_release_upgrade(container, *, database: str, database_url: str) -> None:
    _bootstrap_current_head(database_url)
    _project_head_to_session_v2_parent(container, database=database)
    _assert_session_v2_parent_projection(container, database=database)
    _alembic_upgrade(database_url, "head")


def _alembic_downgrade(database_url: str, target: str) -> None:
    env = {**os.environ, "DATABASE_URL": database_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", target],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic downgrade {target} failed:\n"
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
def revision_parent_migrated_pg_url(pg_container) -> str:  # noqa: F811  (pytest fixture param)
    """Prove the newest revision from its exact projected parent using normal Alembic."""
    code, output = pg_container.exec(
        ["psql", "-U", "test", "-d", "postgres", "-c", "CREATE DATABASE revisionparenttest"]
    )
    if code != 0:
        pytest.fail(f"failed to create revisionparenttest database: {output}")

    # NB: str(URL) masks the password as '***' — must render explicitly.
    async_url = (
        make_url(_async_url(pg_container)).set(database="revisionparenttest").render_as_string(hide_password=False)
    )

    _prepare_session_v2_release_upgrade(
        pg_container,
        database="revisionparenttest",
        database_url=async_url,
    )
    return async_url


@pytest.fixture(scope="session")
def session_v2_input_control_parent_migrated_pg_url(pg_container) -> str:  # noqa: F811
    """Exercise the exact ``session_v2_0716`` parent → current-head edge.

    The ordinary release fixture proves the complete Session V2 revision from
    its projected parent.  This dedicated database additionally downgrades an
    empty current schema to the immediate parent of the input/control delta,
    verifies that parent shape, then performs an ordinary upgrade to head.
    """

    code, output = pg_container.exec(
        ["psql", "-U", "test", "-d", "postgres", "-c", "CREATE DATABASE sessionv2inputcontrolparent"]
    )
    if code != 0:
        pytest.fail(f"failed to create sessionv2inputcontrolparent database: {output}")
    async_url = (
        make_url(_async_url(pg_container))
        .set(database="sessionv2inputcontrolparent")
        .render_as_string(hide_password=False)
    )
    _bootstrap_current_head(async_url)
    _alembic_downgrade(async_url, "session_v2_0716")
    query = """
      SELECT
        (SELECT version_num FROM alembic_version),
        (SELECT is_nullable FROM information_schema.columns
          WHERE table_schema='public' AND table_name='session_turn_replacements'
            AND column_name='cancel_control_id'),
        (SELECT is_nullable FROM information_schema.columns
          WHERE table_schema='public' AND table_name='session_turn_replacements'
            AND column_name='cancel_command_id')
    """
    code, output = pg_container.exec(
        ["psql", "-U", "test", "-d", "sessionv2inputcontrolparent", "-At", "-F", "|", "-c", query]
    )
    if code != 0:
        pytest.fail(f"failed to inspect Session V2 input/control parent: {output}")
    evidence = (output.decode() if isinstance(output, bytes) else str(output)).strip()
    if evidence != "session_v2_0716|NO|NO":
        pytest.fail(f"input/control parent projection is not exact: {evidence!r}")
    _alembic_upgrade(async_url, "head")
    return async_url


@pytest.fixture(scope="session")
def session_v2_admission_revision_parent_pg_url(pg_container) -> str:  # noqa: F811
    """Dedicated immediate-parent database for admission revision backfill.

    The fixture stops at ``session_v2_input_control_0716`` so the test can
    insert a production-shaped legacy revision-3 input with its single
    immutable admission attempt before executing the ordinary head upgrade.
    """

    code, output = pg_container.exec(
        ["psql", "-U", "test", "-d", "postgres", "-c", "CREATE DATABASE sessionv2admissionrevisionparent"]
    )
    if code != 0:
        pytest.fail(f"failed to create sessionv2admissionrevisionparent database: {output}")
    async_url = (
        make_url(_async_url(pg_container))
        .set(database="sessionv2admissionrevisionparent")
        .render_as_string(hide_password=False)
    )
    _bootstrap_current_head(async_url)
    _alembic_downgrade(async_url, "session_v2_input_control_0716")
    query = """
      SELECT
        (SELECT version_num FROM alembic_version),
        (SELECT count(*) FROM information_schema.columns
          WHERE table_schema='public' AND table_name='session_input_admissions'
            AND column_name='input_revision'),
        (SELECT count(*) FROM pg_constraint
          WHERE conrelid='session_input_admissions'::regclass
            AND conname='uq_session_input_admissions_input')
    """
    code, output = pg_container.exec(
        [
            "psql",
            "-U",
            "test",
            "-d",
            "sessionv2admissionrevisionparent",
            "-At",
            "-F",
            "|",
            "-c",
            query,
        ]
    )
    if code != 0:
        pytest.fail(f"failed to inspect Session V2 admission parent: {output}")
    evidence = (output.decode() if isinstance(output, bytes) else str(output)).strip()
    if evidence != "session_v2_input_control_0716|0|1":
        pytest.fail(f"admission revision parent projection is not exact: {evidence!r}")
    return async_url


@pytest.fixture(scope="session")
def session_v2_roundtrip_pg_url(pg_container) -> str:  # noqa: F811  (pytest fixture injection)
    """Dedicated parent→head database for downgrade/re-upgrade evidence tests."""

    code, output = pg_container.exec(
        ["psql", "-U", "test", "-d", "postgres", "-c", "CREATE DATABASE sessionv2roundtrip"]
    )
    if code != 0:
        pytest.fail(f"failed to create sessionv2roundtrip database: {output}")
    async_url = (
        make_url(_async_url(pg_container)).set(database="sessionv2roundtrip").render_as_string(hide_password=False)
    )
    _prepare_session_v2_release_upgrade(
        pg_container,
        database="sessionv2roundtrip",
        database_url=async_url,
    )
    return async_url


@pytest.fixture(scope="session")
def session_v2_invalid_index_pg_url(pg_container) -> str:  # noqa: F811  (pytest fixture injection)
    """Dedicated head database for invalid concurrent-index residue recovery."""

    code, output = pg_container.exec(
        ["psql", "-U", "test", "-d", "postgres", "-c", "CREATE DATABASE sessionv2invalidindex"]
    )
    if code != 0:
        pytest.fail(f"failed to create sessionv2invalidindex database: {output}")
    async_url = (
        make_url(_async_url(pg_container)).set(database="sessionv2invalidindex").render_as_string(hide_password=False)
    )
    _prepare_session_v2_release_upgrade(
        pg_container,
        database="sessionv2invalidindex",
        database_url=async_url,
    )
    return async_url
