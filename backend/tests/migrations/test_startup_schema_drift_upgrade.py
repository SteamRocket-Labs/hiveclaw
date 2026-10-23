from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import BACKEND_ROOT, _async_url


def _run_alembic(
    database_url: str,
    command: str,
    target: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.mark.asyncio
async def test_versioned_database_with_startup_create_all_can_apply_blocked_revision(pg_container) -> None:
    """Reproduce the production drift caused by startup create_all before Alembic.

    The database first reaches the last pre-release revision through the real
    migration chain.  Current model metadata is then applied exactly as the
    runtime startup used to do: existing tables are left untouched while new
    release tables are created early.  The first blocked Alembic revision must
    reconcile its empty bootstrap table and still execute its backfills, policy,
    trigger, and version advance.
    """

    database_name = f"startup_drift_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)

    # Bootstrap current metadata, then reconstruct the first blocked revision's
    # preconditions.  Repository history predates Alembic and therefore cannot
    # be replayed from an empty schema; this focused setup exercises the exact
    # production collision without relying on incomplete legacy downgrades.
    bootstrap = _run_alembic(database_url, "upgrade", "head")
    assert bootstrap.returncode == 0, bootstrap.stderr[-4000:]

    from app.database import Base
    from app.models import import_all_models

    import_all_models()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text("DROP TRIGGER IF EXISTS trg_config_revision_immutability ON config_revisions")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS enforce_config_revision_immutability()"))
            await connection.execute(text("DROP INDEX IF EXISTS ix_config_revisions_parent_revision_id"))
            await connection.execute(text("DROP INDEX IF EXISTS ix_config_revisions_rollback_of_revision_id"))
            await connection.execute(text("ALTER TABLE config_revisions DROP COLUMN IF EXISTS parent_revision_id"))
            await connection.execute(text("ALTER TABLE config_revisions DROP COLUMN IF EXISTS rollback_of_revision_id"))
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": "runtime_assembly_nested_0710"},
            )
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            early_table_count = (await connection.execute(text("SELECT count(*) FROM ai_asset_records"))).scalar_one()
    finally:
        await engine.dispose()

    assert version == "runtime_assembly_nested_0710"
    assert early_table_count == 0

    upgrade = _run_alembic(database_url, "upgrade", "ai_asset_control_plane_0710")
    assert upgrade.returncode == 0, f"stdout tail: {upgrade.stdout[-4000:]}\nstderr tail: {upgrade.stderr[-4000:]}"

    verify_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with verify_engine.connect() as connection:
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            trigger_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgname = 'trg_config_revision_immutability' AND NOT tgisinternal"
                    )
                )
            ).scalar_one()
    finally:
        await verify_engine.dispose()

    assert version == "ai_asset_control_plane_0710"
    assert trigger_count == 1


@pytest.mark.asyncio
async def test_release_policy_migrations_replay_against_startup_created_policies(pg_container) -> None:
    database_name = f"startup_policy_drift_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)

    bootstrap = _run_alembic(database_url, "upgrade", "head")
    assert bootstrap.returncode == 0, bootstrap.stderr[-4000:]

    tables = (
        "workspace_resource_manifests",
        "workflow_promotion_proposals",
        "budget_transition_outbox",
    )
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('ai_asset_usage_events_0711')")
            )
            policies_before = (
                await connection.execute(
                    text("SELECT count(*) FROM pg_policies WHERE tablename = ANY(:tables)"),
                    {"tables": list(tables)},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert policies_before == 3
    upgrade = _run_alembic(database_url, "upgrade", "budget_transition_outbox_0711")
    assert upgrade.returncode == 0, f"stdout tail: {upgrade.stdout[-4000:]}\nstderr tail: {upgrade.stderr[-4000:]}"

    verify_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with verify_engine.connect() as connection:
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            policy_rows = (
                await connection.execute(
                    text(
                        "SELECT tablename, count(*) FROM pg_policies "
                        "WHERE tablename = ANY(:tables) GROUP BY tablename ORDER BY tablename"
                    ),
                    {"tables": list(tables)},
                )
            ).all()
    finally:
        await verify_engine.dispose()

    assert version == "budget_transition_outbox_0711"
    assert policy_rows == sorted((table, 1) for table in tables)


@pytest.mark.asyncio
async def test_docker_table_stage_leaves_versioned_schema_to_alembic(pg_container) -> None:
    database_name = f"docker_schema_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    bootstrap = _run_alembic(database_url, "upgrade", "head")
    assert bootstrap.returncode == 0, bootstrap.stderr[-4000:]

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE content_input_receipts CASCADE"))
            await connection.execute(
                text("UPDATE alembic_version SET version_num = 'session_v2_delete_fk_indexes_0907'")
            )
    finally:
        await engine.dispose()

    entrypoint = (BACKEND_ROOT / "entrypoint.sh").read_text()
    table_stage = entrypoint.split("DATABASE_URL=\"$SCHEMA_URL\" python << 'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    result = subprocess.run(
        [sys.executable, "-B", "-"],
        input=table_stage,
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('content_input_receipts')")) is None
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "session_v2_delete_fk_indexes_0907"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_identity_upgrade_adopts_startup_tables_without_losing_rows(pg_container) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.organization_identity import BusinessTeam
    from app.models.tenant import Tenant
    from app.models.user import User

    database_name = f"identity_drift_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    bootstrap = _run_alembic(database_url, "upgrade", "head")
    assert bootstrap.returncode == 0, bootstrap.stderr[-4000:]
    tenant_id, user_id, team_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(Tenant(id=tenant_id, name="Startup schema recovery", slug=f"drift-{tenant_id.hex}"))
            await session.flush()
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"drift-{user_id.hex}",
                    email=f"{user_id.hex}@example.test",
                    display_name="Migration owner",
                    password_hash="synthetic-hash",
                    role="member",
                )
            )
            await session.flush()
            session.add(
                BusinessTeam(
                    id=team_id, tenant_id=tenant_id, name="Retained team", status="active", created_by_user_id=user_id
                )
            )
            await session.execute(text("UPDATE alembic_version SET version_num = 'session_v2_delete_fk_indexes_0907'"))
            await session.commit()
    finally:
        await engine.dispose()

    upgrade = _run_alembic(database_url, "upgrade", "m1b_identity_links_1015")
    assert upgrade.returncode == 0, upgrade.stderr[-4000:]
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "m1b_identity_links_1015"
            assert (
                await connection.execute(
                    text("SELECT id, tenant_id, name, status, created_by_user_id FROM business_teams")
                )
            ).one() == (team_id, tenant_id, "Retained team", "active", user_id)
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_class WHERE relname = ANY(:tables) "
                        "AND relrowsecurity AND relforcerowsecurity"
                    ),
                    {
                        "tables": [
                            "business_teams",
                            "business_team_memberships",
                            "org_member_user_links",
                            "department_directory_links",
                            "identity_cutover_journal",
                            "content_input_receipts",
                        ]
                    },
                )
                == 6
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("premature_tables", [False, True], ids=["released-schema", "startup-drift"])
async def test_released_schema_with_premature_tables_upgrades_to_head(pg_container, premature_tables: bool) -> None:
    import gzip
    import hashlib
    import json

    import asyncpg
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import Base
    from app.models import import_all_models
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.scripts.verify_schema_readiness import inspect_schema_readiness

    fixture = BACKEND_ROOT / "tests" / "fixtures" / "runtime_schema_0907.sql.gz"
    metadata = json.loads(fixture.with_name("runtime_schema_0907.json").read_text())
    sql = gzip.decompress(fixture.read_bytes())
    assert hashlib.sha256(sql).hexdigest() == metadata["sql_sha256"]
    assert metadata["revision"] == "session_v2_delete_fk_indexes_0907"
    database_name = f"released_drift_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    connection = await asyncpg.connect(
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    try:
        await connection.execute("DROP SCHEMA public CASCADE")
        await connection.execute(sql.decode())
    finally:
        await connection.close()

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(Tenant(id=tenant_id, name="Released schema", slug=f"released-{tenant_id.hex}"))
            await session.flush()
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"released-{user_id.hex}",
                    email=f"{user_id.hex}@example.test",
                    display_name="Retained owner",
                    password_hash="synthetic-hash",
                    role="member",
                )
            )
            await session.commit()
        async with engine.begin() as connection:
            before = (
                (await connection.execute(text("SELECT row_to_json(u)::text FROM users u ORDER BY id"))).scalars().all()
            )
            assert await connection.scalar(text("SELECT to_regclass('business_teams')")) is None
            if premature_tables:
                import_all_models()
                await connection.run_sync(Base.metadata.create_all)
                assert await connection.scalar(text("SELECT to_regclass('business_teams')")) is not None
    finally:
        await engine.dispose()

    upgrade = _run_alembic(database_url, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr[-4000:]
    head = ScriptDirectory.from_config(Config(str(BACKEND_ROOT / "alembic.ini"))).get_current_head()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == head
            after = (
                (await connection.execute(text("SELECT row_to_json(u)::text FROM users u ORDER BY id"))).scalars().all()
            )
            assert after == before
            report = await inspect_schema_readiness(connection)
            assert report.ready, report.issues
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_docker_fresh_bootstrap_reaches_verified_head(pg_container) -> None:
    from app.scripts.verify_schema_readiness import inspect_schema_readiness

    database_name = f"docker_fresh_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    entrypoint = (BACKEND_ROOT / "entrypoint.sh").read_text()
    table_stage = entrypoint.split("DATABASE_URL=\"$SCHEMA_URL\" python << 'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    result = subprocess.run(
        [sys.executable, "-B", "-"],
        input=table_stage,
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    bootstrap = _run_alembic(database_url, "upgrade", "head")
    assert bootstrap.returncode == 0, bootstrap.stderr[-4000:]
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            report = await inspect_schema_readiness(connection)
            assert report.ready, report.issues
    finally:
        await engine.dispose()
