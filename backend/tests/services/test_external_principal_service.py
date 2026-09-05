from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database import enter_rls_bypass, tenant_scoped_session
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
        assert unlinked.channel_identity_invalidated is True
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
        unbind_receipt = await unlink_external_principal(
            db,
            tenant_id=tenant_id,
            principal_id=linked.principal.id,
            actor_user_id=owner_id,
            reason="admin revoked compromised WeChat binding",
        )
        assert unbind_receipt.channel_identity_invalidated is True
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
async def test_unbound_principal_unlink_leaves_healthy_manual_channel_configuration_intact(owner_sessionmaker):
    """EXTERNAL-PRINCIPAL-UNBOUND-UNLINK-001 regression (real PostgreSQL).

    A never-bound guest principal and a manually configured Feishu channel
    both carry a ``None`` self identity, so ``None == None`` must not be read
    as "the channel was bound to this principal". Unlinking the guest is an
    idempotent no-op for channel configuration while the principal's own
    session/message attribution is still cleaned up.
    """
    tenant_id, owner_id, agent_id = await _seed_tenant_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = ChannelConfig(
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel_type="feishu",
            is_configured=True,
            is_connected=True,
            extra_config={"setup_method": "manual_app_credentials"},
        )
        db.add(config)
        await db.flush()
        resolved = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider="feishu",
            installation_ref=str(config.id),
            channel_config_id=config.id,
            subject_id="ou_never_bound_guest",
            display_name="Never Bound Guest",
        )
        session = ChatSession(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=owner_id,
            external_principal_id=resolved.principal.id,
            title="Guest session awaiting attribution cleanup",
            source_channel="feishu",
            external_conv_id="feishu-unbound-guest-session",
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
            user_id=owner_id,
            external_principal_id=resolved.principal.id,
            role="user",
            content="hello from an unbound guest",
            conversation_id=str(session.id),
        )
        db.add(message)
        await db.commit()
        principal_id, config_id, session_id, message_id = resolved.principal.id, config.id, session.id, message.id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        unlinked = await unlink_external_principal(
            db,
            tenant_id=tenant_id,
            principal_id=principal_id,
            actor_user_id=owner_id,
            reason="unbound guest unlink must stay a configuration no-op",
        )
        await db.commit()

    assert unlinked.actor.id is None
    assert unlinked.actor.authority_bound is False
    assert unlinked.channel_identity_invalidated is False

    async with owner_sessionmaker() as db:
        stored_config = await db.get(ChannelConfig, config_id)
        stored_principal = await db.get(ExternalPrincipal, principal_id)
        stored_session = await db.get(ChatSession, session_id)
        stored_message = await db.get(ChatMessage, message_id)
        unlink_events = (
            await db.execute(
                select(func.count())
                .select_from(ExternalPrincipalBindingEvent)
                .where(
                    ExternalPrincipalBindingEvent.external_principal_id == principal_id,
                    ExternalPrincipalBindingEvent.action == "unlinked",
                )
            )
        ).scalar_one()

    assert stored_config is not None and stored_config.is_configured is True
    assert stored_config.is_connected is True
    assert stored_config.self_identity_user_id is None
    assert stored_config.self_identity_verified_at is None
    assert "connection_status" not in (stored_config.extra_config or {})
    assert "identity_status" not in (stored_config.extra_config or {})
    assert stored_principal is not None and stored_principal.linked_user_id is None
    assert stored_session is not None and stored_session.user_id is None
    assert stored_message is not None and stored_message.user_id is None
    assert unlink_events == 0


@pytest.mark.usefixtures("migrated_pg_url")
async def test_bypass_unlink_rejects_cross_tenant_channel_config_pointer(owner_sessionmaker):
    tenant_a, owner_a, _agent_a = await _seed_tenant_agent(owner_sessionmaker)
    tenant_b, _owner_b, agent_b = await _seed_tenant_agent(owner_sessionmaker)

    async with owner_sessionmaker() as db:
        config = ChannelConfig(
            tenant_id=tenant_b,
            agent_id=agent_b,
            channel_type="feishu",
            is_configured=True,
            is_connected=True,
            self_identity_user_id=owner_a,
            self_identity_verified_at=datetime.now(UTC),
            extra_config={"setup_method": "qr_registration"},
        )
        db.add(config)
        await db.flush()
        now = datetime.now(UTC)
        principal_id = uuid.uuid4()
        db.add(
            ExternalPrincipal(
                id=principal_id,
                tenant_id=tenant_a,
                provider="feishu",
                installation_ref=str(config.id),
                channel_config_id=config.id,
                subject_id="ou_cross_tenant_pointer",
                display_name="Cross-tenant pointer",
                linked_user_id=owner_a,
                linked_at=now,
                binding_method="feishu_qr",
                binding_verified_at=now,
            )
        )
        await db.commit()
        config_id = config.id

    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="cross-tenant unlink boundary probe") as bypass_db:
            with pytest.raises(
                ExternalPrincipalAuthorityError,
                match="external principal channel configuration tenant mismatch",
            ):
                await unlink_external_principal(
                    bypass_db,
                    tenant_id=tenant_a,
                    principal_id=principal_id,
                    actor_user_id=owner_a,
                    reason="tenant retirement authority revocation",
                )
            in_memory_principal = await bypass_db.get(ExternalPrincipal, principal_id)
            in_memory_config = await bypass_db.get(ChannelConfig, config_id)
            assert in_memory_principal is not None and in_memory_principal.linked_user_id == owner_a
            assert in_memory_principal.binding_method == "feishu_qr"
            assert in_memory_config is not None and in_memory_config.tenant_id == tenant_b
            assert in_memory_config.self_identity_user_id == owner_a
            assert in_memory_config.is_connected is True
        await db.rollback()

    async with owner_sessionmaker() as db:
        stored_principal = await db.get(ExternalPrincipal, principal_id)
        stored_config = await db.get(ChannelConfig, config_id)
        unlink_events = (
            await db.execute(
                select(func.count())
                .select_from(ExternalPrincipalBindingEvent)
                .where(
                    ExternalPrincipalBindingEvent.external_principal_id == principal_id,
                    ExternalPrincipalBindingEvent.action == "unlinked",
                )
            )
        ).scalar_one()

    assert stored_principal is not None
    assert stored_principal.linked_user_id == owner_a
    assert stored_principal.binding_method == "feishu_qr"
    assert stored_principal.binding_verified_at is not None
    assert stored_config is not None and stored_config.tenant_id == tenant_b
    assert stored_config.self_identity_user_id == owner_a
    assert stored_config.self_identity_verified_at is not None
    assert stored_config.is_connected is True
    assert stored_config.is_configured is True
    assert "connection_status" not in (stored_config.extra_config or {})
    assert "identity_status" not in (stored_config.extra_config or {})
    assert unlink_events == 0


@pytest.mark.usefixtures("migrated_pg_url")
async def test_bypass_unlink_rebinds_only_same_tenant_chat_messages(owner_sessionmaker):
    tenant_a, owner_a, agent_a = await _seed_tenant_agent(owner_sessionmaker)
    tenant_b, owner_b, agent_b = await _seed_tenant_agent(owner_sessionmaker)

    async with owner_sessionmaker() as db:
        now = datetime.now(UTC)
        principal_id = uuid.uuid4()
        db.add(
            ExternalPrincipal(
                id=principal_id,
                tenant_id=tenant_a,
                provider="feishu",
                installation_ref="tenant-a-installation",
                subject_id="ou_same_tenant_messages",
                display_name="Tenant A Channel User",
                linked_user_id=owner_a,
                linked_at=now,
                binding_method="feishu_qr",
                binding_verified_at=now,
            )
        )
        await db.flush()
        same_tenant_message = ChatMessage(
            tenant_id=tenant_a,
            agent_id=agent_a,
            user_id=owner_a,
            external_principal_id=principal_id,
            role="user",
            content="same-tenant channel message",
            conversation_id="tenant-a-conversation",
        )
        # The principal FK alone is representable on a foreign-tenant row, so
        # an audited unlink must keep the tenant predicate on the bulk update.
        foreign_message = ChatMessage(
            tenant_id=tenant_b,
            agent_id=agent_b,
            user_id=owner_b,
            external_principal_id=principal_id,
            role="assistant",
            content="foreign-tenant sentinel message",
            conversation_id="tenant-b-conversation",
        )
        db.add(same_tenant_message)
        db.add(foreign_message)
        await db.commit()

    async with owner_sessionmaker() as db:
        seeded_foreign = await db.get(ChatMessage, foreign_message.id)
        assert seeded_foreign is not None
        foreign_snapshot = {column: getattr(seeded_foreign, column) for column in ChatMessage.__table__.columns.keys()}

    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="cross-tenant unlink message boundary probe") as bypass_db:
            await unlink_external_principal(
                bypass_db,
                tenant_id=tenant_a,
                principal_id=principal_id,
                actor_user_id=owner_a,
                reason="tenant-scoped chat message unlink",
            )
            await bypass_db.commit()
        await db.rollback()

    async with owner_sessionmaker() as db:
        stored_same_tenant = await db.get(ChatMessage, same_tenant_message.id)
        stored_foreign = await db.get(ChatMessage, foreign_message.id)
        assert stored_same_tenant is not None
        assert stored_foreign is not None
        foreign_after = {column: getattr(stored_foreign, column) for column in ChatMessage.__table__.columns.keys()}

    assert stored_same_tenant.user_id is None
    assert stored_same_tenant.external_principal_id == principal_id
    assert foreign_after == foreign_snapshot


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


async def _seed_admin_channel_world(owner_sessionmaker):
    """Tenant A (employee owner + org admin + member) and a separate platform home tenant."""

    tenant_id = uuid.uuid4()
    home_tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    org_admin_id = uuid.uuid4()
    member_id = uuid.uuid4()
    platform_admin_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Admin Channel Tenant", slug=f"adm-{tenant_id.hex[:12]}"))
        db.add(Tenant(id=home_tenant_id, name="Platform Home", slug=f"plat-{home_tenant_id.hex[:12]}"))

        def _user(user_id, name, role, home):
            return User(
                id=user_id,
                username=f"{name}-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@external.test",
                password_hash="x",
                display_name=name,
                tenant_id=home,
                role=role,
                is_active=True,
            )

        db.add(_user(owner_id, "employee-owner", "member", tenant_id))
        db.add(_user(org_admin_id, "company-admin", "org_admin", tenant_id))
        db.add(_user(member_id, "plain-member", "member", tenant_id))
        db.add(_user(platform_admin_id, "platform-admin", "platform_admin", home_tenant_id))
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Employee WeChat Agent",
                role_description="serve channel guests",
                creator_id=owner_id,
                owner_user_id=owner_id,
                sponsor_user_id=owner_id,
            )
        )
        await db.commit()
    return tenant_id, owner_id, org_admin_id, member_id, platform_admin_id, agent_id


async def _feishu_config(db, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> ChannelConfig:
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
    return config


@pytest.mark.usefixtures("migrated_pg_url")
async def test_org_admin_cannot_bind_employee_self_channel_on_their_behalf(owner_sessionmaker):
    """The only configuration lane is the provider-authenticated connect flow.

    An administrator configures a managed Agent's channel with the
    administrator's own provider-authenticated identity; binding an
    employee's identity on their behalf stays a typed refusal (PDEC-013).
    """
    tenant_id, owner_id, org_admin_id, _member_id, _platform_id, agent_id = await _seed_admin_channel_world(
        owner_sessionmaker
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = await _feishu_config(db, tenant_id, agent_id)
        with pytest.raises(ExternalPrincipalAuthorityError, match="authenticated installer"):
            await bind_authenticated_self_channel_principal(
                db,
                tenant_id=tenant_id,
                config=config,
                provider_subject_id="ou_employee_scan",
                user_id=owner_id,
                actor_user_id=org_admin_id,
            )
        await db.rollback()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_platform_admin_cannot_bind_employee_self_channel_on_their_behalf(owner_sessionmaker):
    tenant_id, owner_id, _org_admin_id, _member_id, platform_admin_id, agent_id = await _seed_admin_channel_world(
        owner_sessionmaker
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = await _feishu_config(db, tenant_id, agent_id)
        with pytest.raises(ExternalPrincipalAuthorityError, match="authenticated installer"):
            await bind_authenticated_self_channel_principal(
                db,
                tenant_id=tenant_id,
                config=config,
                provider_subject_id="ou_employee_scan_platform",
                user_id=owner_id,
                actor_user_id=platform_admin_id,
            )
        await db.rollback()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_member_cannot_bind_someone_elses_self_channel(owner_sessionmaker):
    tenant_id, owner_id, _org_admin_id, member_id, _platform_id, agent_id = await _seed_admin_channel_world(
        owner_sessionmaker
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = await _feishu_config(db, tenant_id, agent_id)
        with pytest.raises(ExternalPrincipalAuthorityError):
            await bind_authenticated_self_channel_principal(
                db,
                tenant_id=tenant_id,
                config=config,
                provider_subject_id="ou_member_forged",
                user_id=owner_id,
                actor_user_id=member_id,
            )
        await db.rollback()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_platform_admin_self_identity_in_foreign_company_is_typed_refusal(owner_sessionmaker):
    """A platform administrator's own inbound identity cannot live in a foreign
    tenant-owned principal; the narrow typed recovery keeps tenant integrity."""

    tenant_id, _owner_id, _org_admin_id, _member_id, platform_admin_id, agent_id = await _seed_admin_channel_world(
        owner_sessionmaker
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = await _feishu_config(db, tenant_id, agent_id)
        with pytest.raises(ExternalPrincipalAuthorityError, match="active same-tenant user"):
            await bind_authenticated_self_channel_principal(
                db,
                tenant_id=tenant_id,
                config=config,
                provider_subject_id="ou_platform_own_identity",
                user_id=platform_admin_id,
                actor_user_id=platform_admin_id,
            )
        await db.rollback()
