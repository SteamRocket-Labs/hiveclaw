"""A populated legacy department keeps its exact identity during upgrade."""

from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models.org import OrgDepartment
from app.models.tenant import Tenant

from tests.integration.conftest import _async_url
from tests.migrations.test_m1d_directory_observation_migration import _alembic


async def test_namespaced_departments_preserve_legacy_and_refuse_lossy_downgrade(
    pg_container,
):
    database_name = f"directory_namespace_{uuid4().hex}"
    code, output = pg_container.exec(
        [
            "psql",
            "-U",
            "test",
            "-d",
            "postgres",
            "-c",
            f"CREATE DATABASE {database_name}",
        ]
    )
    assert code == 0, output
    url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic(url, "upgrade", "head")
    _alembic(url, "downgrade", "workbench_projects_1019")
    tenant, legacy, new, provider = uuid4(), uuid4(), uuid4(), uuid4()
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as db:
            await db.execute(text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
            await db.execute(insert(Tenant).values(id=tenant, name="Legacy directory", slug=tenant.hex))
            await db.execute(
                text(
                    "INSERT INTO org_departments(id, tenant_id, feishu_id, name, path, member_count) VALUES (:id, :tenant, 'legacy-feishu-id', 'Original department', 'Original path', 7)"
                ),
                {"id": legacy, "tenant": tenant},
            )
    finally:
        await engine.dispose()
    _alembic(url, "upgrade", "head")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as db:
            await db.execute(text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
            row = (
                await db.execute(
                    text(
                        "SELECT feishu_id, name, path, member_count, provider_id, provider_department_id FROM org_departments WHERE id=:id"
                    ),
                    {"id": legacy},
                )
            ).one()
            assert tuple(row) == (
                "legacy-feishu-id",
                "Original department",
                "Original path",
                7,
                None,
                None,
            )
            await db.execute(
                text(
                    "INSERT INTO identity_providers(id, tenant_id, provider_type, name, config, is_active, sso_login_enabled) VALUES (:id, :tenant, 'wecom', 'WeCom', '{}', true, false)"
                ),
                {"id": provider, "tenant": tenant},
            )
            await db.execute(
                insert(OrgDepartment).values(
                    id=new,
                    tenant_id=tenant,
                    provider_id=provider,
                    provider_department_id="1",
                    name="Observed department",
                )
            )
        with pytest.raises(AssertionError, match="Retain provider directory provenance"):
            _alembic(url, "downgrade", "workbench_projects_1019")
        async with engine.begin() as db:
            await db.execute(text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
            assert (
                await db.scalar(
                    text("SELECT provider_id FROM org_departments WHERE id=:id"),
                    {"id": new},
                )
                == provider
            )
    finally:
        await engine.dispose()
