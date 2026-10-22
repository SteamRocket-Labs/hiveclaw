"""Compatibility history uses the same source authority and recovery as the catalog."""

from contextlib import nullcontext
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.database import tenant_scoped_session
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4g_learned_skill_sources_postgres import (
    _assert_native_asset_state,
    controlled_provider_boundary,  # noqa: F401
    learned_skill,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.fixture
async def legacy_skill(learned_skill, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import config_history

    s, _submission = learned_skill
    n = s.n
    payload = await _assert_native_asset_state(n, app_user_sessionmaker,
        evolution_state='provisional', lifecycle_status='active')
    n.client._transport.app.include_router(config_history.router)
    monkeypatch.setattr(n.client._transport, 'raise_app_exceptions', False)
    return s, payload


async def _use_reader(s, reader, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.core.security import get_current_user
    from app.models.user import User

    n = s.n
    user_id = n.foreign if reader in {'other_member', 'recipient_admin'} else n.user
    if reader != 'other_member':
        async with owner_sessionmaker() as db:
            (await db.get(User, user_id)).role = 'org_admin'
            await db.commit()

    async def current_user():
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            return await db.get(User, user_id)

    monkeypatch.setitem(n.client._transport.app.dependency_overrides, get_current_user, current_user)


@pytest.mark.parametrize('reader', ['author', 'other_member', 'recipient_admin'])
async def test_legacy_history_keeps_the_learned_source_audience(
    legacy_skill, reader, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.core.source_read_purpose import source_recipient_read

    s, payload = legacy_skill
    n = s.n
    await _use_reader(s, reader, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    asset_id = payload['asset']['id']
    purpose = source_recipient_read(session_id=s.second_session, user_id=n.foreign) if reader == 'recipient_admin' else nullcontext()
    with purpose:
        canonical = [await n.client.get(f'/enterprise/ai-assets/{asset_id}/revisions{suffix}') for suffix in ('', '/1')]
        legacy = [await n.client.get(f'/config-history/ai_asset/{asset_id}{suffix}') for suffix in ('', '/1')]
    expected = 200 if reader == 'author' else 403
    assert [response.status_code for response in canonical] == [expected, expected]
    assert [response.status_code for response in legacy] == [expected, expected], [response.text for response in legacy]
    if reader == 'author':
        assert [response.json() for response in legacy] == [response.json() for response in canonical]
        assert 'EVIDENCE_METHOD_VERSION_ONE' in legacy[1].text
    else:
        assert all('EVIDENCE_METHOD_VERSION_ONE' not in response.text for response in legacy)


@pytest.mark.parametrize('reader', ['owner_admin', 'missing_command', 'recipient_admin'])
async def test_legacy_rollback_keeps_exact_owner_commands_and_typed_denial(
    legacy_skill, reader, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.core.source_read_purpose import source_recipient_read
    from app.models.ai_asset import AIAssetRecord
    from app.models.config_revision import ConfigRevision
    from app.services.agent_asset_transaction import read_agent_asset_revision

    s, payload = legacy_skill
    n = s.n
    await _use_reader(s, reader, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    asset_id = payload['asset']['id']
    before = read_agent_asset_revision(n.root)
    body = (n.root / n.first_learned_skill_path).read_bytes()
    command = {'target_version': 1}
    if reader != 'missing_command':
        command.update(operation_id=str(uuid4()), expected_revision_id=payload['asset']['active_revision_id'])
    purpose = source_recipient_read(session_id=s.second_session, user_id=n.foreign) if reader == 'recipient_admin' else nullcontext()
    with purpose:
        legacy = await n.client.post(f'/config-history/ai_asset/{asset_id}/rollback', json=command)
    expected = 200 if reader == 'owner_admin' else 409 if reader == 'missing_command' else 403
    assert legacy.status_code == expected, legacy.text
    if reader == 'owner_admin':
        assert legacy.json() == {'version': 2, 'message': 'Rolled back to version 1'}
        native_after = read_agent_asset_revision(n.root)
        assert native_after == before + 1
        repeated = await n.client.post(f'/config-history/ai_asset/{asset_id}/rollback', json=command)
        modern = await n.client.post(f'/enterprise/ai-assets/{asset_id}/rollback', json=command)
        assert repeated.status_code == modern.status_code == 200
        assert repeated.json() == legacy.json() and modern.json()['version'] == legacy.json()['version']
        assert read_agent_asset_revision(n.root) == native_after
    else:
        assert legacy.json()['detail']['code'] == (
            'skill_owner_command_required' if reader == 'missing_command' else 'learned_skill_source_denied')
        assert read_agent_asset_revision(n.root) == before
    assert (n.root / n.first_learned_skill_path).read_bytes() == body
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        asset = await db.get(AIAssetRecord, UUID(asset_id))
        assert asset.projection_status == 'applied'
        if reader != 'owner_admin':
            assert str(asset.active_revision_id) == payload['asset']['active_revision_id']
        operations = await db.scalar(select(func.count()).select_from(ConfigRevision).where(
            ConfigRevision.tenant_id == n.tenant, ConfigRevision.entity_type == 'learned_skill_asset_operation',
            ConfigRevision.is_active.is_(True)))
        assert operations == (1 if reader == 'owner_admin' else 0)
