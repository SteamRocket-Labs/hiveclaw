"""Real-PostgreSQL integration fixtures (Testcontainers) — §9 P0 foundation.

Route-level principle (docs/workflow-source-capability.md §9): anything that
touches migrations, RLS policies, journals, advisory locks, worker leases or
cross-worker resume must be verified against a real PostgreSQL; mock sessions
are only allowed for pure logic. This conftest provides:

* ``pg_container`` — one PostgreSQL container per test session;
* ``migrated_pg_url`` — the container URL after the FULL alembic chain ran,
  plus a non-superuser ``rls_app_user`` role (the only kind of role that
  ENABLE-only RLS actually applies to — production connects as the table
  owner, which silently bypasses ENABLE-only policies);
* ``owner_engine`` / ``app_user_engine`` — async engines for both roles;
* ``owner_sessionmaker`` — factory for ``tenant_scoped_session`` tests.

The whole directory is skipped when Docker is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]

APP_USER = "rls_app_user"
# Throwaway container-local credential — generated fresh per test session so
# no secret lives in source; the container is destroyed after the run anyway.
APP_USER_PASSWORD = uuid.uuid4().hex


def _ensure_docker_host() -> None:
    """docker-py (used by testcontainers) does NOT read docker CLI contexts.

    On macOS Docker Desktop the daemon socket lives at
    ``~/.docker/run/docker.sock`` and the ``/var/run/docker.sock`` symlink is
    often absent — point DOCKER_HOST at it so the SDK can connect.
    """
    if os.environ.get("DOCKER_HOST"):
        return
    if Path("/var/run/docker.sock").exists():
        return
    desktop_sock = Path.home() / ".docker" / "run" / "docker.sock"
    if desktop_sock.exists():
        os.environ["DOCKER_HOST"] = f"unix://{desktop_sock}"


@pytest.fixture(scope="session")
def pg_container():
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover — dev extra missing
        pytest.skip("testcontainers is not installed (pip install -e '.[dev]')")

    _ensure_docker_host()
    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # Docker daemon missing or not running
        pytest.skip(f"Docker unavailable for Testcontainers: {exc}")
    yield container
    container.stop()


def _async_url(container) -> str:
    return container.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")


@pytest.fixture(scope="session")
def migrated_pg_url(pg_container) -> str:
    """Run the real alembic chain once against the container, create the
    non-owner role, and hand out the asyncpg URL."""
    url = _async_url(pg_container)
    env = {**os.environ, "DATABASE_URL": url}
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
            "alembic upgrade head failed against the integration container:\n"
            f"stdout tail: {result.stdout[-2000:]}\n"
            f"stderr tail: {result.stderr[-2000:]}"
        )

    # Non-owner, non-superuser, NOBYPASSRLS role: the only kind of role
    # ENABLE-only RLS actually filters. Privileges mirror an app user.
    create_role_sql = (
        f"DO $$ BEGIN CREATE ROLE {APP_USER} LOGIN PASSWORD '{APP_USER_PASSWORD}' "
        "NOSUPERUSER NOBYPASSRLS; EXCEPTION WHEN duplicate_object THEN NULL; END $$; "
        f"GRANT USAGE ON SCHEMA public TO {APP_USER}; "
        f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {APP_USER}; "
        f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {APP_USER};"
    )
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "test", "-c", create_role_sql])
    if code != 0:
        pytest.fail(f"failed to create {APP_USER} role: {output}")
    return url


@pytest.fixture()
async def owner_engine(migrated_pg_url):
    """Engine connected as the migration-running owner — mirrors production's
    single DATABASE_URL user (table owner)."""
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture()
async def app_user_engine(migrated_pg_url):
    """Engine connected as the non-owner ``rls_app_user`` — RLS applies here."""
    url = make_url(migrated_pg_url).set(username=APP_USER, password=APP_USER_PASSWORD)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture()
def owner_sessionmaker(owner_engine):
    return async_sessionmaker(owner_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture()
def app_user_sessionmaker(app_user_engine):
    """Sessions as the non-superuser role — the ONLY kind of connection RLS
    (even FORCEd) actually filters. The container's default ``test`` user is
    a superuser and bypasses every policy, exactly like production's
    ``hive`` (the POSTGRES_USER init user) — switching the app to a
    non-superuser role is the P15 deployment task."""
    return async_sessionmaker(app_user_engine, class_=AsyncSession, expire_on_commit=False)
