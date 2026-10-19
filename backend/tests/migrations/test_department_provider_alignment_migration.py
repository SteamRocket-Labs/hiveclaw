"""Upgrade old mapping/journal rows without relabeling their provider namespace."""

from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment
from app.models.organization_identity import DepartmentDirectoryLink, IdentityCutoverJournal
from app.models.tenant import Tenant
from app.models.user import Department, User
from tests.integration.conftest import _async_url
from tests.migrations.test_m1d_directory_observation_migration import _alembic


async def test_mapping_upgrade_retains_legacy_journal_and_blocks_lossy_downgrade(pg_container):
    name = f'provider_alignment_{uuid4().hex[:8]}'
    code, output = pg_container.exec(['psql', '-U', 'test', '-d', 'postgres', '-c', f'CREATE DATABASE {name}'])
    assert code == 0, output
    url = make_url(_async_url(pg_container)).set(database=name).render_as_string(hide_password=False)
    _alembic(url, 'upgrade', 'head')
    _alembic(url, 'downgrade', 'company_directory_namespace_1019')
    tenant, admin, department, mirror, mapping, journal = [uuid4() for _ in range(6)]
    legacy_after = {'org_department_id': str(mirror), 'department_id': str(department),
        'provider': 'feishu', 'provider_department_id': f'legacy-{mirror.hex}', 'status': 'active'}
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as db:
            await db.execute(text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
            await db.execute(insert(Tenant).values(id=tenant, name='Legacy alignment', slug=tenant.hex))
            await db.execute(insert(User).values(id=admin, tenant_id=tenant, username=admin.hex,
                email=f'{admin.hex}@fixture.test', password_hash='synthetic', display_name='Migration admin', role='org_admin'))
            await db.execute(insert(Department).values(id=department, tenant_id=tenant, name='Legacy department'))
            await db.execute(insert(OrgDepartment).values(id=mirror, tenant_id=tenant, name='Legacy mirror', feishu_id=f'legacy-{mirror.hex}'))
            await db.execute(text('INSERT INTO department_directory_links(id, tenant_id, org_department_id, department_id, provider, provider_department_id, status) VALUES (:id, :tenant, :mirror, :department, :provider, :subject, :status)'),
                {'id': mapping, 'tenant': tenant, 'mirror': mirror, 'department': department,
                    'provider': 'feishu', 'subject': legacy_after['provider_department_id'], 'status': 'active'})
            await db.execute(insert(IdentityCutoverJournal).values(id=journal, tenant_id=tenant,
                object_kind='department_directory_link', object_id=mapping, before_json=None, after_json=legacy_after))
    finally:
        await engine.dispose()
    _alembic(url, 'upgrade', 'head')
    engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            row = await db.get(DepartmentDirectoryLink, mapping)
            assert (row.provider, row.provider_id, row.provider_department_id) == ('feishu', None, legacy_after['provider_department_id'])
            assert (await db.get(IdentityCutoverJournal, journal)).after_json == legacy_after
            from app.services.identity_cutover import rollback_cutover
            restored = await rollback_cutover(db, tenant_id=tenant, actor_id=admin, journal_ids=[journal])
            assert restored['rows'][0]['outcome'] == 'restored', restored
            await db.commit()
            assert (await db.get(DepartmentDirectoryLink, mapping)).status == 'revoked'
            assert (await db.get(IdentityCutoverJournal, journal)).after_json == legacy_after
            provider = IdentityProvider(tenant_id=tenant, provider_type='wecom', name='WeCom', is_active=True, config={})
            db.add(provider)
            await db.flush()
            observed = OrgDepartment(tenant_id=tenant, provider_id=provider.id, provider_department_id='1', name='Observed')
            db.add(observed)
            await db.flush()
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    db.add(DepartmentDirectoryLink(tenant_id=tenant, org_department_id=observed.id,
                        department_id=department, provider='wecom', provider_department_id='1', status='active'))
                    await db.flush()
            db.add(DepartmentDirectoryLink(tenant_id=tenant, org_department_id=observed.id, department_id=department,
                provider='wecom', provider_id=provider.id, provider_department_id='1', status='active'))
            await db.commit()
        with pytest.raises(AssertionError, match='Retain provider department mappings'):
            _alembic(url, 'downgrade', 'company_directory_namespace_1019')
        async with sessions() as db:
            assert (await db.get(IdentityCutoverJournal, journal)).after_json == legacy_after
            assert await db.scalar(text("SELECT count(*) FROM department_directory_links WHERE provider='wecom' AND provider_id IS NOT NULL")) == 1
    finally:
        await engine.dispose()
