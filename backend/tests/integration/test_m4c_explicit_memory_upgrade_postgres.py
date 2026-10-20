"""Existing memory input receipts survive the real additive migration."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import tenant_scoped_session
from app.models.organization_identity import ContentInputReceipt
from app.models.tenant import Tenant
from tests.integration import conftest as persistence
from tests.integration.test_m4a_knowledge_analysis_upgrade_postgres import migration_database, upgrade


def revision():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/explicit_memory_sources_1021.py"
    spec = importlib.util.spec_from_file_location("explicit_memory_migration_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def downgrade(connection):
    with Operations.context(MigrationContext.configure(connection)):
        revision().downgrade()


async def test_existing_receipts_upgrade_with_rls_and_populated_downgrade_preserves_all_tenants(
    pg_container, app_user_engine,
):
    tenant, other = uuid4(), uuid4()
    old_id, scoped_id = uuid4(), uuid4()
    async with migration_database(pg_container) as url:
        upgrade(url)
        engine = create_async_engine(url)
        try:
            async with engine.begin() as db:
                await db.run_sync(downgrade)
                await db.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": revision().down_revision})
                for identity in (tenant, other):
                    await db.execute(Tenant.__table__.insert().values(id=identity, name="Memory upgrade",
                        slug="memory-upgrade-" + identity.hex))
                await db.execute(text("INSERT INTO content_input_receipts(id,tenant_id,carrier_kind,carrier_ref,revision,content_sha256,dependencies_json) "
                    "VALUES(:id,:tenant,'team_memory','legacy-source',1,:hash,'[]'::jsonb)"),
                    {"id": old_id, "tenant": tenant, "hash": "a" * 64})
            upgrade(url)
            async with engine.begin() as db:
                legacy = (await db.execute(text("SELECT carrier_ref,content_sha256,authority_json FROM content_input_receipts WHERE id=:id"),
                    {"id": old_id})).one()
                assert legacy == ("legacy-source", "a" * 64, {})
                flags = (await db.execute(text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='content_input_receipts'"))).one()
                assert flags == (True, True)
                await db.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{persistence.APP_USER}"'))
                await db.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{persistence.APP_USER}"'))
            app_engine = create_async_engine(app_user_engine.url.set(database=engine.url.database))
            factory = async_sessionmaker(app_engine, expire_on_commit=False)
            try:
                async with tenant_scoped_session(other, session_factory=factory) as db:
                    assert not await db.scalar(text("SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user"))
                    db.add(ContentInputReceipt(id=scoped_id, tenant_id=other, carrier_kind="explicit_memory",
                        carrier_ref="synthetic-agent\nexplicit-upgrade", revision=1, content_sha256="b" * 64,
                        dependencies_json=[], authority_json={"schema": "migration_fixture", "state": "prepared"}))
                    await db.commit()
                for scope, expected in ((str(tenant), [old_id]), (str(other), [scoped_id]), ("", [])):
                    async with factory() as db:
                        await db.execute(text("SELECT set_config('app.current_tenant_id',:scope,true)"), {"scope": scope})
                        assert list((await db.scalars(text("SELECT id FROM content_input_receipts"))).all()) == expected
                async with engine.begin() as db:
                    await db.execute(text("SELECT set_config('app.current_tenant_id',:scope,true)"), {"scope": str(tenant)})
                    with pytest.raises(RuntimeError, match="require_recovery_before_downgrade"):
                        await db.run_sync(downgrade)
                    assert await db.scalar(text("SELECT current_setting('app.current_tenant_id')")) == str(tenant)
                    assert await db.scalar(text("SELECT count(*) FROM content_input_receipts")) == 2
                # Only test-owned synthetic rows are removed to verify the
                # compatible rollback and upgrade; real source data blocks it.
                async with engine.begin() as db:
                    await db.execute(text("DELETE FROM content_input_receipts WHERE id=:id"), {"id": scoped_id})
                    await db.run_sync(downgrade)
                    await db.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": revision().down_revision})
                upgrade(url)
                async with engine.begin() as db:
                    assert await db.scalar(text("SELECT content_sha256 FROM content_input_receipts WHERE id=:id"),
                        {"id": old_id}) == "a" * 64
            finally:
                await app_engine.dispose()
        finally:
            await engine.dispose()
