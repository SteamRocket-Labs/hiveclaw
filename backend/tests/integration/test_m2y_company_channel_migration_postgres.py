"""Populated upgrades preserve existing identity and never discard channel proofs."""

import os
import subprocess
import sys
from uuid import uuid4

import httpx
from sqlalchemy import select, text

from app.database import get_db, pin_rls_tenant_context
from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal
from app.models.tenant_channel_config import CompanyChannelBindingChallenge
from tests.integration import conftest as _database_fixtures
from tests.integration import test_m2y_company_channel_identity_postgres as _company_fixtures

company_messaging = _company_fixtures.company_messaging
company_wecom_request = _company_fixtures.company_wecom_request

# This test owns a schema transition, so it gets a separate fixture database.
app_user_engine = _database_fixtures.app_user_engine
app_user_sessionmaker = _database_fixtures.app_user_sessionmaker
migrated_pg_url = _database_fixtures.migrated_pg_url
owner_engine = _database_fixtures.owner_engine
owner_sessionmaker = _database_fixtures.owner_sessionmaker
pg_container = _database_fixtures.pg_container


async def test_populated_upgrade_preserves_verified_identity_and_retains_proofs_after_config_removal(
    company_messaging, migrated_pg_url, owner_sessionmaker, app_user_sessionmaker,
):
    seed = company_messaging
    def migrate(*args):
        return subprocess.run([sys.executable, '-B', '-m', 'alembic', *args],
            env={**os.environ, 'DATABASE_URL': migrated_pg_url}, capture_output=True, text=True, timeout=60)
    previous = migrate('downgrade', 'department_provider_links_1019')
    assert previous.returncode == 0, previous.stderr
    legacy_config, legacy_principal = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChannelConfig(id=legacy_config, tenant_id=seed['tenant_id'], agent_id=seed['agent_id'],
            channel_type='feishu', app_id='synthetic-legacy-app', app_secret='synthetic-legacy-secret', is_configured=True))
        await db.flush()
        await db.execute(text("INSERT INTO external_principals (id,tenant_id,provider,installation_ref,channel_config_id,"
            "subject_id,display_name,linked_user_id,binding_method,linked_at,binding_verified_at) "
            "VALUES (:id,:tenant,'feishu',:installation,:config,'ou_legacy','Legacy',:user,'feishu_qr',now(),now())"),
            {'id': legacy_principal, 'tenant': seed['tenant_id'], 'installation': str(legacy_config),
                'config': legacy_config, 'user': seed['user_id']})
        await db.commit()
    upgraded = migrate('upgrade', 'head')
    assert upgraded.returncode == 0, upgraded.stderr
    async with owner_sessionmaker() as db:
        legacy = await db.get(ExternalPrincipal, legacy_principal)
        assert legacy.linked_user_id == seed['user_id'] and legacy.binding_method == 'feishu_qr'
        assert legacy.channel_config_id == legacy_config and legacy.tenant_channel_config_id is None
        assert (await db.execute(text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
            "WHERE relname='company_channel_binding_challenges'"))).scalar_one() is True
    # Match the production entrypoint: migrated tables get DML grants before
    # the non-owner application is started. The fixture role predates upgrade.
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    password = uuid4().hex
    granted = subprocess.run([sys.executable, '-B', '-m', 'app.scripts.grant_rls_app_role'],
        env={**os.environ, 'DATABASE_URL': migrated_pg_url, 'SCHEMA_DATABASE_URL': migrated_pg_url,
            'RLS_APP_PASSWORD': password}, capture_output=True, text=True, timeout=60)
    assert granted.returncode == 0, granted.stderr
    application_url = make_url(migrated_pg_url).set(username='app_rls', password=password)
    application_engine = create_async_engine(application_url, poolclass=NullPool)
    application_sessions = async_sessionmaker(application_engine, expire_on_commit=False)
    async def application_database():
        async with application_sessions() as db:
            yield db
            await db.commit()
    seed['app'].dependency_overrides[get_db] = application_database
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert issued.status_code == 200, issued.text
        body, params = company_wecom_request(seed['configs']['wecom'], text=issued.json()['command'])
        bound = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", content=body, params=params)
        assert bound.status_code == 200, bound.text
    async with application_sessions() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        proof = await db.scalar(select(CompanyChannelBindingChallenge).where(CompanyChannelBindingChallenge.user_id == seed['user_id']))
        assert proof is not None and proof.consumed_at is not None and proof.principal_id is not None
        await pin_rls_tenant_context(db, uuid4())
        assert await db.get(CompanyChannelBindingChallenge, proof.id, populate_existing=True) is None
    refused = migrate('downgrade', 'department_provider_links_1019')
    assert refused.returncode != 0 and 'Retain company binding proofs' in refused.stderr
    async with owner_sessionmaker() as db:
        assert (await db.execute(text('SELECT version_num FROM alembic_version'))).scalar_one() == 'company_channel_binding_1019'
    seed['actor']['user'] = seed['admin']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        removed = await api.delete('/tenant-channels/wecom')
        assert removed.status_code == 204, removed.text
    async with owner_sessionmaker() as db:
        preserved = await db.get(CompanyChannelBindingChallenge, proof.id, populate_existing=True)
        assert (await db.get(ExternalPrincipal, proof.principal_id)).linked_user_id is None
        assert preserved.config_id is None and preserved.principal_id == proof.principal_id and preserved.consumed_at == proof.consumed_at
        assert (await db.get(ExternalPrincipal, legacy_principal)).binding_method == 'feishu_qr'

    await application_engine.dispose()
