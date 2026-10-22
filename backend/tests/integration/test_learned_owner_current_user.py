"""Learning owner commands preserve selected-company identity and live authority."""

import asyncio
from uuid import UUID, uuid4, uuid5

from fastapi import Request
import pytest
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import pin_rls_tenant_context
from app.models.ai_asset import AIAssetRecord
from app.models.config_revision import ConfigRevision
from app.models.tenant import Tenant
from app.models.user import User
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4g_learned_skill_sources_postgres import (
    controlled_provider_boundary,  # noqa: F401
    learned_skill,  # noqa: F401
)
from tests.integration.test_legacy_learned_asset_history import legacy_skill  # noqa: F401

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize(('home', 'change', 'failure'), [
    ('global', None, None), ('foreign', None, None), ('same', None, None),
    ('global', 'role', None), ('global', 'inactive', None), ('foreign', 'home_inactive', None),
    ('same', 'role', None), ('same', 'inactive', None),
    ('global', None, 'prepare_after'), ('foreign', None, 'prepare_after'),
])
async def test_learned_rollback_reloads_current_selected_company_owner(
    legacy_skill, owner_sessionmaker, app_user_sessionmaker, monkeypatch, home, change, failure,  # noqa: F811
):
    from app.core.security import create_access_token, decode_access_token, get_current_user
    from app.database import get_db
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.learned_skill_asset_rollback import ENTITY

    s, payload = legacy_skill
    n = s.n
    asset_id = UUID(payload['asset']['id'])
    actor_id, operation_id = uuid4(), uuid4()
    home_id = None if home == 'global' else n.tenant if home == 'same' else uuid4()
    role = 'org_admin' if home == 'same' else 'platform_admin'
    async with owner_sessionmaker() as db:
        if home == 'foreign':
            db.add(Tenant(id=home_id, name='Administrator home', slug=uuid4().hex))
            await db.flush()
        db.add(User(id=actor_id, tenant_id=home_id, username=uuid4().hex, email=f'{uuid4().hex}@synthetic.invalid',
            password_hash='synthetic', display_name='Learning administrator', role=role, is_active=True))
        await db.commit()
    waiting, observed = asyncio.Event(), {'prepare_reply_lost': False}

    class ObservedSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            if (isinstance(statement, Select) and statement._for_update_arg is not None and not waiting.is_set()
                and any(column.get('entity') is AIAssetRecord for column in statement.column_descriptions)):
                observed['pid'] = await super().scalar(text('SELECT pg_backend_pid()'))
                waiting.set()
            return await super().scalar(statement, *args, **kwargs)

        async def commit(self):
            pending = None
            if failure and not observed['prepare_reply_lost']:
                pending = await super().scalar(select(ConfigRevision.id).where(ConfigRevision.tenant_id == n.tenant,
                    ConfigRevision.entity_type == ENTITY, ConfigRevision.entity_id == uuid5(asset_id, str(operation_id)),
                    ConfigRevision.is_active.is_(True), ConfigRevision.content['state'].astext == 'pending'))
            await super().commit()
            if pending is not None:
                observed['prepare_reply_lost'] = True
                raise RuntimeError('Committed owner prepare response was lost')

    factory = async_sessionmaker(app_user_sessionmaker.kw['bind'], class_=ObservedSession, expire_on_commit=False)

    async def selected_db(request: Request):
        claims = decode_access_token(request.headers['authorization'].removeprefix('Bearer '))
        selected = request.headers.get('x-tenant-id') if claims['role'] == 'platform_admin' else None
        async with factory() as db:
            await pin_rls_tenant_context(db, UUID(selected or claims['tid']))
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    app = n.client._transport.app
    app.dependency_overrides.pop(get_current_user)
    monkeypatch.setitem(app.dependency_overrides, get_db, selected_db)
    monkeypatch.setattr('app.services.audit_logger.async_session', app_user_sessionmaker)
    n.client.headers.update({'Authorization': 'Bearer ' + create_access_token(str(actor_id), role,
        str(home_id) if home_id is not None else None), 'X-Tenant-Id': str(n.tenant)})
    url = f'/enterprise/ai-assets/{asset_id}/rollback'
    command = {'target_version': 1, 'operation_id': str(operation_id),
        'expected_revision_id': payload['asset']['active_revision_id']}
    before = read_agent_asset_revision(n.root)
    body = (n.root / n.first_learned_skill_path).read_bytes()
    async with owner_sessionmaker() as blocker:
        await blocker.execute(select(AIAssetRecord.id).where(AIAssetRecord.id == asset_id).with_for_update())
        blocker_pid = await blocker.scalar(text('SELECT pg_backend_pid()'))
        consumer = asyncio.create_task(n.client.post(url, json=command))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=10)
            async with owner_sessionmaker() as changer:
                await changer.execute(text(f"""DO $test$
                    DECLARE deadline timestamptz := clock_timestamp() + interval '3 seconds';
                    BEGIN
                        WHILE NOT {int(blocker_pid)} = ANY(pg_blocking_pids({int(observed['pid'])})) LOOP
                            IF clock_timestamp() > deadline THEN RAISE EXCEPTION 'Owner did not reach row lock'; END IF;
                            PERFORM pg_sleep(0.01);
                        END LOOP;
                    END $test$"""))
                assert blocker_pid in await changer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['pid']})
                if change == 'role':
                    (await changer.get(User, actor_id)).role = 'member'
                elif change == 'inactive':
                    (await changer.get(User, actor_id)).is_active = False
                elif change == 'home_inactive':
                    (await changer.get(Tenant, home_id)).is_active = False
                await changer.commit()
            await blocker.rollback()
            response = await asyncio.wait_for(consumer, timeout=20)
        finally:
            await blocker.rollback()
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
    if change:
        assert response.status_code == 403 and response.json()['detail']['code'] == 'skill_owner_authority_required', response.text
        assert read_agent_asset_revision(n.root) == before
    else:
        assert response.status_code == 200, response.text
        assert read_agent_asset_revision(n.root) == before + 1
        repeated = await n.client.post(url, json=command)
        assert repeated.status_code == 200 and repeated.json() == response.json(), repeated.text
        assert read_agent_asset_revision(n.root) == before + 1
        assert observed['prepare_reply_lost'] is (failure is not None)
    assert (n.root / n.first_learned_skill_path).read_bytes() == body
    async with owner_sessionmaker() as db:
        actor = await db.get(User, actor_id)
        assert (actor.tenant_id, actor.role, actor.is_active) == (
            home_id, 'member' if change == 'role' else role, change != 'inactive')
        record = await db.get(AIAssetRecord, asset_id)
        assert record.projection_status == 'applied'
        if change:
            assert str(record.active_revision_id) == command['expected_revision_id']
        completed = await db.scalar(select(func.count()).select_from(ConfigRevision).where(
            ConfigRevision.tenant_id == n.tenant, ConfigRevision.entity_type == ENTITY,
            ConfigRevision.entity_id == uuid5(asset_id, str(operation_id)), ConfigRevision.is_active.is_(True)))
        assert completed == (0 if change else 1)
