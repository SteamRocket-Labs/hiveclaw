from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal, ExternalPrincipalBindingEvent
from app.models.tenant import Tenant
from app.models.user import User
from app.services.external_principal_service import (
    bind_authenticated_self_channel_principal,
    ExternalPrincipalAuthorityError,
    ExternalPrincipalRevokedError,
    load_external_runtime_actor,
    resolve_or_create_external_principal,
    revoke_channel_config_external_principals,
    revoke_external_principals_for_installation,
    unlink_external_principal,
)


async def _seed_tenant_agent(owner_sessionmaker):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="External Tenant", slug=f"external-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                username=f"owner-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@external.test",
                password_hash="x",
                display_name="External Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="External Agent",
                role_description="serve channel guests",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.commit()
    return tenant_id, user_id, agent_id


@pytest.mark.usefixtures("migrated_pg_url")
async def test_unbound_external_principal_is_idempotent_without_creating_user(owner_sessionmaker):
    tenant_id, _owner_id, _agent_id = await _seed_tenant_agent(owner_sessionmaker)
    before_users = 0
    async with owner_sessionmaker() as db:
        before_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        first = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider="slack",
            installation_ref="config-a",
            subject_id="U123",
            display_name="Alice",
            profile={"team_id": "T1"},
        )
        second = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider="SLACK",
            installation_ref="config-a",
            subject_id="U123",
            display_name="Alice Updated",
            profile={"team_id": "T1", "locale": "zh-CN"},
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        after_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        principals = (
            (
                await db.execute(
                    select(ExternalPrincipal).where(
                        ExternalPrincipal.tenant_id == tenant_id,
                        ExternalPrincipal.provider == "slack",
                        ExternalPrincipal.installation_ref == "config-a",
                        ExternalPrincipal.subject_id == "U123",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert first.principal.id == second.principal.id
    assert first.actor.id is None
    assert first.actor.external_principal_id == first.principal.id
    assert first.actor.authority_bound is False
    assert after_users == before_users
    assert len(principals) == 1
    assert principals[0].display_name == "Alice Updated"
    assert principals[0].profile_json["locale"] == "zh-CN"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_identity_scope_includes_tenant_and_installation(owner_sessionmaker):
    tenant_a, _owner_a, _agent_a = await _seed_tenant_agent(owner_sessionmaker)
    tenant_b, _owner_b, _agent_b = await _seed_tenant_agent(owner_sessionmaker)

    ids = []
    for tenant_id, installation_ref in (
        (tenant_a, "config-a"),
        (tenant_a, "config-b"),
        (tenant_b, "config-a"),
    ):
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            resolved = await resolve_or_create_external_principal(
                db,
                tenant_id=tenant_id,
                provider="telegram",
                installation_ref=installation_ref,
                subject_id="same-provider-subject",
                display_name="Same Person",
            )
            ids.append(resolved.principal.id)
            await db.commit()

    assert len(set(ids)) == 3


@pytest.mark.usefixtures("migrated_pg_url")
async def test_database_rejects_binding_proof_from_the_wrong_provider(owner_sessionmaker):
    tenant_id, owner_id, _agent_id = await _seed_tenant_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        now = datetime.now(UTC)
        db.add(
            ExternalPrincipal(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                provider="slack",
                installation_ref="slack-installation",
                subject_id="U-proof-mismatch",
                display_name="Invalid binding",
                linked_user_id=owner_id,
                linked_at=now,
                binding_method="feishu_qr",
                binding_verified_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_qr_self_binding_and_admin_unlink_are_audited_authority_transitions(owner_sessionmaker):
    tenant_id, owner_id, agent_id = await _seed_tenant_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = ChannelConfig(
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel_type="feishu",
            is_configured=True,
            is_connected=True,
            extra_config={"setup_method": "qr_registration"},
        )
        db.add(config)
        await db.flush()
        linked = await bind_authenticated_self_channel_principal(
            db,
            tenant_id=tenant_id,
            config=config,
            provider_subject_id="ou_installer",
            user_id=owner_id,
            actor_user_id=owner_id,
        )
        await db.commit()

    assert linked.actor.id == owner_id
    assert linked.actor.authority_bound is True
    assert linked.principal.binding_method == "feishu_qr"
    assert linked.principal.binding_verified_at is not None

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        actor = await load_external_runtime_actor(
            db,
            tenant_id=tenant_id,
            principal_id=linked.principal.id,
            expected_user_id=owner_id,
        )
        unlinked = await unlink_external_principal(
            db,
            tenant_id=tenant_id,
            principal_id=linked.principal.id,
            actor_user_id=owner_id,
            reason="account disconnected",
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        stored_config = await db.get(ChannelConfig, config.id)
        events = (
            (
                await db.execute(
                    select(ExternalPrincipalBindingEvent)
                    .where(ExternalPrincipalBindingEvent.external_principal_id == linked.principal.id)
                    .order_by(ExternalPrincipalBindingEvent.created_at)
                )
            )
            .scalars()
            .all()
        )

    assert actor.id == owner_id
    assert unlinked.actor.id is None
    assert [event.action for event in events] == ["linked", "unlinked"]
    assert events[0].new_user_id == owner_id
    assert events[1].previous_user_id == owner_id
    assert stored_config is not None and stored_config.is_connected is False
    assert stored_config.is_configured is False
    assert stored_config.self_identity_user_id is None
    assert stored_config.extra_config["connection_status"] == "identity_rebind_required"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_link_and_unlink_atomically_rebind_existing_channel_sessions(owner_sessionmaker):
    tenant_id, owner_id, agent_id = await _seed_tenant_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = ChannelConfig(
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel_type="feishu",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )
        db.add(config)
        await db.flush()
        resolved = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider="feishu",
            installation_ref=str(config.id),
            channel_config_id=config.id,
            subject_id="ou_existing_session",
            display_name="Existing Feishu User",
        )
        session = ChatSession(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=None,
            external_principal_id=resolved.principal.id,
            title="Existing channel session",
            source_channel="feishu",
            external_conv_id="feishu-existing-session",
            session_kind="human_chat",
            actor_type="external_principal",
            runtime_source="channel_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
        )
        db.add(session)
        await db.flush()
        message = ChatMessage(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=None,
            external_principal_id=resolved.principal.id,
            role="user",
            content="hello before binding",
            conversation_id=str(session.id),
        )
        db.add(message)
        await db.commit()
        principal_id, config_id, session_id, message_id = resolved.principal.id, config.id, session.id, message.id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await bind_authenticated_self_channel_principal(
            db,
            tenant_id=tenant_id,
            config=await db.get(ChannelConfig, config_id),
            provider_subject_id="ou_existing_session",
            user_id=owner_id,
            actor_user_id=owner_id,
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        linked_session = await db.get(ChatSession, session_id)
        linked_message = await db.get(ChatMessage, message_id)
        assert linked_session is not None and linked_session.user_id == owner_id
        assert linked_message is not None and linked_message.user_id == owner_id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await unlink_external_principal(
            db,
            tenant_id=tenant_id,
            principal_id=principal_id,
            actor_user_id=owner_id,
            reason="unlink existing channel session",
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        unlinked_session = await db.get(ChatSession, session_id)
        unlinked_message = await db.get(ChatMessage, message_id)
        assert unlinked_session is not None and unlinked_session.user_id is None
        assert unlinked_message is not None and unlinked_message.user_id is None

        denied_user_id = uuid.uuid4()
        db.add(
            User(
                id=denied_user_id,
                username=f"denied-{denied_user_id.hex[:10]}",
                email=f"{denied_user_id.hex[:12]}@external.test",
                password_hash="x",
                display_name="Denied External User",
                tenant_id=tenant_id,
            )
        )
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(
            ExternalPrincipalAuthorityError,
            match="self channel installation requires Agent manage access",
        ):
            await bind_authenticated_self_channel_principal(
                db,
                tenant_id=tenant_id,
                config=await db.get(ChannelConfig, config_id),
                provider_subject_id="ou_existing_session",
                user_id=denied_user_id,
                actor_user_id=denied_user_id,
            )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_admin_unlink_disconnects_verified_personal_wechat_identity(owner_sessionmaker):
    tenant_id, owner_id, agent_id = await _seed_tenant_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = ChannelConfig(
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel_type="wechat_personal",
            is_configured=True,
            is_connected=True,
            extra_config={"ilink_user_id": "wechat-self"},
        )
        db.add(config)
        await db.flush()
        linked = await bind_authenticated_self_channel_principal(
            db,
            tenant_id=tenant_id,
            config=config,
            provider_subject_id="wechat-self",
            user_id=owner_id,
            actor_user_id=owner_id,
        )
        await unlink_external_principal(
            db,
            tenant_id=tenant_id,
            principal_id=linked.principal.id,
            actor_user_id=owner_id,
            reason="admin revoked compromised WeChat binding",
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        stored_config = await db.get(ChannelConfig, config.id)
        stored_principal = await db.get(ExternalPrincipal, linked.principal.id)

    assert stored_config is not None and stored_config.is_connected is False
    assert stored_config.self_identity_user_id is None
    assert stored_config.self_identity_verified_at is None
    assert stored_principal is not None and stored_principal.linked_user_id is None
    assert stored_principal.binding_method is None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_revoked_installation_fails_closed_and_keeps_history(owner_sessionmaker):
    tenant_id, owner_id, _agent_id = await _seed_tenant_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        resolved = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider="slack",
            installation_ref="deleted-config",
            subject_id="U-revoked",
            display_name="Former Sender",
        )
        count = await revoke_external_principals_for_installation(
            db,
            tenant_id=tenant_id,
            provider="slack",
            installation_ref="deleted-config",
            actor_user_id=owner_id,
            reason="channel config deleted",
        )
        await db.commit()

    assert count == 1
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(ExternalPrincipalRevokedError):
            await resolve_or_create_external_principal(
                db,
                tenant_id=tenant_id,
                provider="slack",
                installation_ref="deleted-config",
                subject_id="U-revoked",
                display_name="Former Sender",
            )
        stored = await db.get(ExternalPrincipal, resolved.principal.id)

    assert stored is not None
    assert stored.status == "revoked"
    assert stored.revoked_at is not None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_channel_config_retirement_revokes_the_exact_installation(owner_sessionmaker):
    tenant_id, owner_id, agent_id = await _seed_tenant_agent(owner_sessionmaker)
    config_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            ChannelConfig(
                id=config_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                channel_type="microsoft_teams",
                is_configured=True,
            )
        )
        await db.flush()
        resolved = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider="teams",
            installation_ref=str(config_id),
            channel_config_id=config_id,
            subject_id="aad-external-user",
            display_name="Teams Guest",
        )
        count = await revoke_channel_config_external_principals(
            db,
            tenant_id=tenant_id,
            config=await db.get(ChannelConfig, config_id),
            actor_user_id=owner_id,
            reason="channel configuration deleted",
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        stored = await db.get(ExternalPrincipal, resolved.principal.id)
        events = (
            (
                await db.execute(
                    select(ExternalPrincipalBindingEvent).where(
                        ExternalPrincipalBindingEvent.external_principal_id == resolved.principal.id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert count == 1
    assert stored is not None and stored.status == "revoked"
    assert events[-1].metadata_json == {
        "provider": "teams",
        "installation_ref": str(config_id),
    }


@pytest.mark.usefixtures("migrated_pg_url")
async def test_external_principal_rls_hides_other_tenant(owner_sessionmaker, app_user_sessionmaker):
    tenant_a, _owner_a, _agent_a = await _seed_tenant_agent(owner_sessionmaker)
    tenant_b, _owner_b, _agent_b = await _seed_tenant_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_a, session_factory=owner_sessionmaker) as db:
        await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_a,
            provider="teams",
            installation_ref="config-a",
            subject_id="external-a",
            display_name="A",
        )
        await db.commit()

    async with tenant_scoped_session(tenant_b, session_factory=app_user_sessionmaker) as db:
        count = (await db.execute(select(func.count()).select_from(ExternalPrincipal))).scalar_one()

    assert count == 0


def test_external_actor_uses_immutable_authority_snapshot_fields():
    from app.services.external_principal_service import ChannelRuntimeActor

    principal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    actor = ChannelRuntimeActor(
        id=None,
        external_principal_id=principal_id,
        tenant_id=tenant_id,
        username="slack:U1",
        display_name="Guest",
        role="external",
        department_id=None,
        authority_bound=False,
        is_active=True,
        authority_snapshot_at=datetime.now(UTC),
    )

    assert actor.id is None
    assert actor.external_principal_id == principal_id
    assert actor.authority_bound is False
