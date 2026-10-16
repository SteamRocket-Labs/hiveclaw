"""Populated company credentials survive the actual owned-account revision."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import APP_USER, _async_url
from tests.migrations.test_m1d_directory_observation_migration import _alembic
from tests.services.test_session_input_control_v2 import _seed_session


async def test_company_upgrade_and_rls_owner_downgrade_preserve_private_credentials(pg_container, migrated_pg_url):
    name = f"mcp_accounts_{uuid.uuid4().hex[:12]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, output
    url = make_url(_async_url(pg_container)).set(database=name).render_as_string(hide_password=False)
    _alembic(url, "upgrade", "head")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        tenant, user, _, _, _ = await _seed_session(async_sessionmaker(engine, expire_on_commit=False))
        _alembic(url, "downgrade", "mcp_oauth_encryption_1016")
        company = uuid.uuid4()
        async with engine.begin() as db:
            columns = await db.run_sync(
                lambda connection: {c["name"] for c in inspect(connection).get_columns("mcp_servers")}
            )
            assert "owner_user_id" not in columns and "parent_server_id" not in columns
            assert not await db.run_sync(lambda connection: inspect(connection).has_table("mcp_account_grants"))
            await db.execute(
                text("""INSERT INTO mcp_servers
                (id, tenant_id, name, server_key, registry_source, status, auth_status, config_json)
                VALUES (:id, :tenant, 'Legacy company documents', 'legacy-company', 'manual',
                    'connected', 'configured', '{"opaque_legacy_config": "unchanged"}'::jsonb)"""),
                {"id": company, "tenant": tenant},
            )
        _alembic(url, "upgrade", "head")
        async with engine.begin() as db:
            kept = (
                await db.execute(
                    text("SELECT owner_user_id, parent_server_id, config_json FROM mcp_servers WHERE id=:id"),
                    {"id": company},
                )
            ).one()
            assert kept.owner_user_id is None and kept.parent_server_id is None
            assert kept.config_json == {"opaque_legacy_config": "unchanged"}
            policies = (
                await db.execute(
                    text("""SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class WHERE relname IN ('mcp_account_grants', 'mcp_work_account_bindings')""")
                )
            ).all()
            assert len(policies) == 2 and all(row.relrowsecurity and row.relforcerowsecurity for row in policies)
            await db.execute(
                text("""INSERT INTO mcp_servers
                (id, tenant_id, owner_user_id, parent_server_id, name, server_key,
                 registry_source, status, auth_status, config_json)
                VALUES (:id, :tenant, :owner, :parent, 'Private documents', 'private-documents',
                    'personal', 'connected', 'configured', '{"opaque_private_config": "retained"}'::jsonb)"""),
                {"id": uuid.uuid4(), "tenant": tenant, "owner": user, "parent": company},
            )
            await db.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {APP_USER}"))
            for table in ("mcp_servers", "mcp_account_grants", "mcp_work_account_bindings"):
                await db.execute(text(f"ALTER TABLE {table} OWNER TO {APP_USER}"))

        spec = importlib.util.spec_from_file_location(
            "owned_account_revision", Path(__file__).parents[2] / "alembic/versions/mcp_owned_accounts_1016.py"
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        def downgrade_as_schema_owner(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()

        async with engine.begin() as db:
            await db.execute(text(f"SET LOCAL ROLE {APP_USER}"))
            await db.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant, true)"), {"tenant": str(uuid.uuid4())}
            )
            assert await db.scalar(text("SELECT count(*) FROM mcp_servers")) == 0
            with pytest.raises(RuntimeError, match="explicitly reconciled"):
                await db.run_sync(downgrade_as_schema_owner)
            assert await db.scalar(text("SELECT count(*) FROM mcp_servers")) == 0
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM mcp_servers WHERE owner_user_id IS NOT NULL")) == 1
            assert await db.run_sync(lambda connection: inspect(connection).has_table("mcp_work_account_bindings"))
    finally:
        await engine.dispose()
