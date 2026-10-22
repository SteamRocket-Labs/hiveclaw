"""Release native business child results only to their current group audience."""

import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.services.channel_delivery_outbox import (
    ChannelDeliveryOutboxService, ChannelDeliveryPermanentError, ChannelDeliveryRetryableError, _claimed,
)
from app.services.feishu_service import FeishuService
from tests.integration.test_business_task_text_sources import (
    original_api, provider, delivered, s3_server,  # noqa: F401
    test_business_task_text_rechecks_its_actual_original_source as _business_source,
)

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
async def test_business_child_result_releases_to_its_original_group(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,  # noqa: F811
):
    async def consume(**context):
        factory, seed, run_id = context['db_factory'], context['seed'], context['run_id']
        service = ChannelDeliveryOutboxService(session_factory=factory)
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            group = await db.get(ChatSession, seed['session_id'])
            private = await db.get(ChatSession, seed['private_session_id'])
            notice = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id))
            assert run.status == 'completed' and run.child_session_id != str(group.id), run.metadata_json
            assert group.audience_kind == 'group' and group.user_id is None
            assert private.audience_kind in {None, 'private'} and private.user_id == seed['user_id']
            assert notice is not None and notice.delivery_target_json['receive_id'] == 'oc_business_group'
            saved = _claimed(notice)
        calls = []
        async def transport(_self, _app_id, _secret, recipient, message_type, content, **kwargs):
            calls.append((recipient, message_type, json.loads(content)['text'], kwargs['receive_id_type']))
            return {'data': {'message_id': 'synthetic-business-group-result'}}
        monkeypatch.setattr(FeishuService, 'send_message', transport)
        counts = await service.drain_once(worker_id='business-group-result')
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            row = await db.get(ChannelDeliveryOutbox, saved.id)
            observation = {'counts': counts, 'status': row.status, 'last_error': row.last_error, 'calls': calls}
            (tmp_path / 'group-result-delivery.json').write_text(json.dumps(observation, indent=2))
            assert counts['delivered'] == 1 and row.status == 'delivered', observation
            assert row.delivery_receipts_json['text']['detail']['provider_message_id'] == 'synthetic-business-group-result'
        assert calls == [('oc_business_group', 'text', saved.text, 'chat_id')]
        assert (await service.drain_once(worker_id='business-group-result'))['claimed'] == 0
        assert len(context['model_client'].requests) == 2
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            with pytest.raises(ChannelDeliveryPermanentError):
                await service._load_delivery_authority_and_artifacts(replace(saved,
                    delivery_target={**saved.delivery_target, 'receive_id': 'oc_another_group'}))
        async def left(*_args, **_kwargs):
            return set()
        with monkeypatch.context() as patch:
            patch.setattr(FeishuService, 'get_chat_member_open_ids', left)
            with pytest.raises(ChannelDeliveryRetryableError):
                await service._load_delivery_authority_and_artifacts(saved)
        from app.models.channel_config import ChannelConfig
        from app.models.security_audit import ResourcePermission
        from app.models.user import User
        from app.services.external_principal_service import bind_authenticated_self_channel_principal

        async with owner_sessionmaker() as db:
            user = User(id=uuid.uuid4(), tenant_id=seed['tenant_id'], username=uuid.uuid4().hex,
                email=f'{uuid.uuid4().hex}@synthetic.invalid', password_hash='synthetic', display_name='New group member')
            db.add(user)
            await db.flush()
            grant = ResourcePermission(tenant_id=seed['tenant_id'], principal_type='user', principal_id=user.id,
                resource_type='agent', resource_id=seed['agent_id'], actions=['read', 'execute', 'manage'], effect='allow')
            db.add(grant)
            config = await db.get(ChannelConfig, seed['group_config_id'])
            await bind_authenticated_self_channel_principal(db, tenant_id=seed['tenant_id'], config=config,
                provider_subject_id='ou-new-group-member', user_id=user.id, actor_user_id=user.id)
            grant.actions = ['read', 'execute']
            await db.commit()
        from fastapi import HTTPException
        from app.api.tasks import _task_delivery_context
        from app.core.permissions import check_agent_access

        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            assert (await check_agent_access(db, user, seed['agent_id']))[1] == 'use'
            with pytest.raises(HTTPException) as private_denied:
                await _task_delivery_context(db, session_id=str(seed['private_session_id']),
                    tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], requester_user_id=user.id)
            assert private_denied.value.status_code == 403
        async def added(*_args, **_kwargs):
            return {'ou-business-owner', 'ou-new-group-member'}
        with monkeypatch.context() as patch:
            patch.setattr(FeishuService, 'get_chat_member_open_ids', added)
            with pytest.raises(ChannelDeliveryPermanentError):
                await service._load_delivery_authority_and_artifacts(saved)
            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                with pytest.raises(ChannelDeliveryPermanentError):
                    await service._authorize_native_result_text(db, saved)
                attempted = []
                async def forbidden_sender(**kwargs):
                    attempted.append(kwargs)
                    raise AssertionError('Revoked group sources reached the provider')
                with pytest.raises(ChannelDeliveryPermanentError):
                    await service._send_part(item=saved, worker_id='business-group-result', part_key='text',
                        sender=forbidden_sender, sender_kwargs={'db': db})
                assert attempted == []
        assert calls == [('oc_business_group', 'text', saved.text, 'chat_id')]
        assert len(context['model_client'].requests) == 2
    await _business_source(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=False, unknown_effect=False, group_delivery_handler=consume)


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('change', ['recipient', 'role', 'configuration'])
async def test_group_delivery_uses_current_facts_in_an_existing_database_session(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, change,  # noqa: F811
):
    from app.models.channel_config import ChannelConfig
    from app.models.external_principal import ExternalPrincipal
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.services.channel_delivery_service import ChannelDeliveryService
    from app.services.external_principal_service import bind_authenticated_self_channel_principal, load_external_runtime_actor
    from app.services.shared_group_authority import load_current_group_members, load_current_group_recipient_users

    async def consume(**context):
        factory, seed = context['db_factory'], context['seed']
        async with owner_sessionmaker() as owner:
            config = await owner.get(ChannelConfig, seed['group_config_id'])
            principal_id = await owner.scalar(select(ExternalPrincipal.id).where(
                ExternalPrincipal.channel_config_id == config.id, ExternalPrincipal.subject_id == 'ou-business-owner'))
            if change == 'recipient':
                other = User(id=uuid.uuid4(), tenant_id=seed['tenant_id'], username=uuid.uuid4().hex,
                    email=f'{uuid.uuid4().hex}@synthetic.invalid', password_hash='synthetic', display_name='Rebound reader')
                owner.add(other)
                await owner.flush()
                owner.add(ResourcePermission(tenant_id=seed['tenant_id'], principal_type='user', principal_id=other.id,
                    resource_type='agent', resource_id=seed['agent_id'], actions=['read', 'execute'], effect='allow'))
                alias = await bind_authenticated_self_channel_principal(owner, tenant_id=seed['tenant_id'], config=config,
                    provider_subject_id='ou-cached-recipient', user_id=seed['user_id'], actor_user_id=seed['user_id'])
                principal_id = alias.principal.id
            elif change == 'role':
                (await owner.get(User, seed['user_id'])).role = 'org_admin'
            await owner.commit()
        seen_apps = []
        async def members(_self, app_id, *_args, **_kwargs):
            seen_apps.append(app_id)
            return {'ou-business-owner', 'ou-cached-recipient'} if change == 'recipient' else {'ou-business-owner'}
        monkeypatch.setattr(FeishuService, 'get_chat_member_open_ids', members)
        async with factory() as db, factory() as delivery_db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            await pin_rls_tenant_context(delivery_db, seed['tenant_id'])
            group = await db.get(ChatSession, seed['session_id'])
            config = await db.get(ChannelConfig, seed['group_config_id'])
            delivery_config = await delivery_db.get(ChannelConfig, config.id)
            cached_principal = await db.get(ExternalPrincipal, principal_id)
            cached_user = await db.get(User, seed['user_id'])
            service = ChannelDeliveryOutboxService(session_factory=factory)
            notice = await db.scalar(select(ChannelDeliveryOutbox).where(
                ChannelDeliveryOutbox.runtime_task_id == context['run_id']))
            saved = _claimed(notice)
            if change == 'recipient':
                assert {u.id for u in (await load_current_group_recipient_users(db, session=group))[1]} == {seed['user_id']}
                await service._authorize_native_result_text(db, saved)
            elif change == 'role':
                assert cached_user.role == 'org_admin'
                before = await load_external_runtime_actor(db, tenant_id=seed['tenant_id'],
                    principal_id=principal_id, expected_user_id=seed['user_id'])
                assert before.authority_bound and before.role == 'org_admin'
            old_app = config.app_id
            async with owner_sessionmaker() as owner:
                if change == 'recipient':
                    (await owner.get(ExternalPrincipal, principal_id)).linked_user_id = other.id
                elif change == 'role':
                    (await owner.get(User, seed['user_id'])).role = 'member'
                else:
                    (await owner.get(ChannelConfig, config.id)).app_id = old_app + '-replaced'
                await owner.commit()
            if change == 'recipient':
                actual = await db.scalar(select(ExternalPrincipal.linked_user_id).where(ExternalPrincipal.id == principal_id))
                assert actual == other.id and cached_principal.linked_user_id == seed['user_id']
                with pytest.raises(ChannelDeliveryPermanentError):
                    await service._authorize_native_result_text(db, saved)
                users = (await load_current_group_recipient_users(db, session=group))[1]
                assert {u.id for u in users} == {seed['user_id'], other.id}
                attempted = []
                async def forbidden_sender(**kwargs):
                    attempted.append(kwargs)
                    raise AssertionError('A stale recipient binding reached the provider')
                with pytest.raises(ChannelDeliveryPermanentError):
                    await service._send_part(item=saved, worker_id='current-group-facts', part_key='text',
                        sender=forbidden_sender, sender_kwargs={'db': db})
                assert attempted == []
            elif change == 'role':
                assert await db.scalar(select(User.role).where(User.id == cached_user.id)) == 'member'
                after = await load_external_runtime_actor(db, tenant_id=seed['tenant_id'],
                    principal_id=principal_id, expected_user_id=seed['user_id'])
                assert after.id == seed['user_id'] and after.authority_bound and after.role == 'member'
                assert cached_user.role == 'member'
            else:
                assert config is not None and delivery_config.app_id == old_app
                await load_current_group_members(db, session=group)
                assert seen_apps == [old_app + '-replaced'], seen_apps
                current = await ChannelDeliveryService._load_target_config(delivery_db, seed['agent_id'], 'feishu', saved.delivery_target)
                assert current.app_id == old_app + '-replaced'
            assert len(context['model_client'].requests) == 2
    await _business_source(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=False, unknown_effect=False, group_delivery_handler=consume)
