"""Recheck current ingress witnesses in a reused application-role session."""

import copy
from uuid import uuid4

import pytest

from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_shared_group_ingress_rejects_changed_witnesses_in_the_same_reader(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch
):
    from app.database import pin_rls_tenant_context
    from app.models.audit import ChatMessage
    from app.models.channel_config import ChannelConfig
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.models.chat_session import ChatSession
    from app.models.external_principal import ExternalPrincipal
    from app.services.channel_ingress_inbox import accept_authenticated_channel_event
    from app.services.channel_session import find_or_create_shared_group_session
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from app.services.shared_group_authority import resolve_shared_group_input_authority

    tenant, user, agent, _, _ = await _seed_session(owner_sessionmaker)
    subject, chat = "ou_current_ingress_reader", "oc_current_ingress_reader"
    body = {
        "event": {"sender": {"sender_id": {"open_id": subject}}, "message": {"chat_type": "group", "chat_id": chat}}
    }
    async with owner_sessionmaker() as db:
        config = ChannelConfig(
            id=uuid4(),
            tenant_id=tenant,
            agent_id=agent,
            channel_type="feishu",
            app_id="synthetic-ingress-app",
            app_secret="synthetic-ingress-secret",
            is_configured=True,
        )
        db.add(config)
        await db.flush()
        actor = (
            await bind_authenticated_self_channel_principal(
                db,
                tenant_id=tenant,
                config=config,
                provider_subject_id=subject,
                user_id=user,
                actor_user_id=user,
            )
        ).actor
        session = await find_or_create_shared_group_session(
            db,
            tenant_id=tenant,
            agent_id=agent,
            installation_ref=str(config.id),
            chat_id=chat,
            first_message_title="Current ingress witnesses",
        )
        receipt = await accept_authenticated_channel_event(
            db,
            tenant_id=tenant,
            agent_id=agent,
            provider="feishu",
            installation_ref=str(config.id),
            provider_event_id=uuid4().hex,
            handler_key="feishu.event",
            body=body,
        )
        event = await db.get(ChannelIngressEvent, receipt.event_id)
        event.status, event.external_principal_id = "processed", actor.external_principal_id
        message = ChatMessage(
            id=uuid4(),
            tenant_id=tenant,
            agent_id=agent,
            user_id=user,
            external_principal_id=actor.external_principal_id,
            conversation_id=str(session.id),
            role="user",
            content="Review this authorized group work.",
            source_ingress_event_id=event.id,
        )
        db.add(message)
        await db.commit()
        session_id, event_id, message_id = session.id, event.id, message.id

    async def members(_self, *_args, **_kwargs):
        return {subject, "ou_another_member"}

    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", members)
    async with app_user_sessionmaker() as reader:
        await pin_rls_tenant_context(reader, tenant)
        current_session = await reader.get(ChatSession, session_id)
        retained_event = await reader.get(ChannelIngressEvent, event_id)
        retained_message = await reader.get(ChatMessage, message_id)
        retained_principal = await reader.get(ExternalPrincipal, actor.external_principal_id)
        arguments = dict(actor=actor, session=current_session, ingress_event_id=event_id)
        authority = await resolve_shared_group_input_authority(reader, **arguments)
        assert authority.principal_id == actor.external_principal_id
        for model, identity, field, value in [
            (ChannelIngressEvent, event_id, "status", "failed"),
            (ChannelIngressEvent, event_id, "installation_ref", "another-installation"),
            (ExternalPrincipal, actor.external_principal_id, "subject_id", "ou_another_member"),
            (ChatMessage, message_id, "user_id", None),
        ]:
            async with owner_sessionmaker() as writer:
                row = await writer.get(model, identity)
                previous = copy.deepcopy(getattr(row, field))
                setattr(row, field, value)
                await writer.commit()
            with pytest.raises(PermissionError, match="shared_group_ingress_mismatch"):
                await resolve_shared_group_input_authority(reader, **arguments)
            async with owner_sessionmaker() as writer:
                setattr(await writer.get(model, identity), field, previous)
                await writer.commit()
            assert (
                await resolve_shared_group_input_authority(reader, **arguments)
            ).principal_id == authority.principal_id
        assert retained_event.status == "processed" and retained_message.user_id == user
        assert retained_principal.subject_id == subject
