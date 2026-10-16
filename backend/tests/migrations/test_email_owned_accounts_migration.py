"""A legacy mailbox stays intact; private account data blocks destructive rollback."""

import importlib.util
from pathlib import Path
import uuid

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


async def test_legacy_mailbox_upgrade_and_schema_owner_rollback_preserve_account_data(pg_container, migrated_pg_url, monkeypatch):
    from app.models.tool import AgentTool, Tool
    from app.services import secrets_provider

    monkeypatch.setattr(secrets_provider, "_provider", secrets_provider.FernetSecretsProvider("synthetic-mailbox-migration-key-only"))
    name = f"mailbox_{uuid.uuid4().hex[:12]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, output
    url = make_url(_async_url(pg_container)).set(database=name).render_as_string(hide_password=False)
    _alembic(url, "upgrade", "head")
    engine = create_async_engine(url, poolclass=NullPool)
    tables = ("email_accounts", "email_account_grants", "email_work_account_bindings")
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        tenant, user, agent, _session, _run = await _seed_session(sessions)
        _alembic(url, "downgrade", "mcp_owned_accounts_1016")
        assignment_id = uuid.uuid4()
        async with sessions() as db:
            tool = Tool(id=uuid.uuid4(), name="send_email", display_name="Legacy email", tenant_id=tenant)
            db.add(tool)
            await db.flush()
            db.add(AgentTool(id=assignment_id, agent_id=agent, tenant_id=tenant, tool_id=tool.id,
                config={"email_address": "legacy@fixture.test", "auth_code": "synthetic-legacy-credential"}))
            await db.commit()
        async with engine.begin() as db:
            assert not await db.run_sync(lambda connection: inspect(connection).has_table("email_accounts"))
            before = await db.scalar(text("SELECT config::text FROM agent_tools WHERE id=:id"), {"id": assignment_id})
        _alembic(url, "upgrade", "head")
        async with engine.begin() as db:
            assert await db.scalar(text("SELECT config::text FROM agent_tools WHERE id=:id"), {"id": assignment_id}) == before
            policies = (await db.execute(text("""SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class WHERE relname IN ('email_accounts','email_account_grants','email_work_account_bindings')"""))).all()
            assert len(policies) == 3 and all(row.relrowsecurity and row.relforcerowsecurity for row in policies)
            await db.execute(text("""INSERT INTO email_accounts(id,tenant_id,owner_user_id,name,config,version)
                VALUES(:id,:tenant,:owner,'Retained mailbox','{"opaque_private_config":"retained"}'::json,1)"""),
                {"id": uuid.uuid4(), "tenant": tenant, "owner": user})
            await db.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {APP_USER}"))
            for table in tables:
                await db.execute(text(f"ALTER TABLE {table} OWNER TO {APP_USER}"))
        spec = importlib.util.spec_from_file_location("mailbox_revision", Path(__file__).parents[2] / "alembic/versions/email_owned_accounts_1017.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        def downgrade_as_owner(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()

        async with engine.begin() as db:
            await db.execute(text(f"SET LOCAL ROLE {APP_USER}"))
            await db.execute(text("SELECT set_config('app.current_tenant_id', :tenant, true)"), {"tenant": str(uuid.uuid4())})
            assert await db.scalar(text("SELECT count(*) FROM email_accounts")) == 0
            with pytest.raises(RuntimeError, match="reconciled"):
                await db.run_sync(downgrade_as_owner)
            assert await db.scalar(text("SELECT count(*) FROM email_accounts")) == 0
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM email_accounts")) == 1
            assert await db.scalar(text("SELECT config->>'opaque_private_config' FROM email_accounts")) == "retained"
    finally:
        await engine.dispose()
