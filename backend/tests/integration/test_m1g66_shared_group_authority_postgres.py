"""Shared input identity and source consumption use live membership, never a session owner."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.chat_session import ChatSession
from app.models.external_principal import ExternalPrincipal
from app.models.security_audit import ResourcePermission
from app.models.session_v2 import SessionCommand
from app.models.user import User
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from tests.services.test_channel_delivery_outbox import _seed_delivery

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_group_command_sources_keep_each_sender_and_recheck_membership(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority, resolve_session_command_authority
    from app.services.channel_ingress_context import use_channel_ingress_context, bind_channel_ingress_external_principal
    from app.services.channel_ingress_inbox import accept_authenticated_channel_event, ChannelIngressInboxService

    seed = await _seed_delivery(owner_sessionmaker, tmp_path, external=True, channel="feishu")
    members = {"sender-1", "sender-2"}
    async def live_members(_self, *_args, **_kwargs):
        return set(members)
    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", live_members, raising=False)
    async with owner_sessionmaker() as db:
        config = await db.get(ChannelConfig, seed["config_id"])
        user1 = await db.get(User, seed["user_id"])
        user2 = User(id=uuid4(), tenant_id=seed["tenant_id"], username=f"second-{uuid4().hex}",
            email=f"{uuid4().hex}@group.test", display_name="Second Member", password_hash="x")
        db.add(user2)
        await db.flush()
        permission = ResourcePermission(tenant_id=seed["tenant_id"], principal_type="user", principal_id=user2.id,
            resource_type="agent", resource_id=seed["agent_id"], actions=["read", "execute", "manage"], effect="allow")
        db.add(permission)
        actors = []
        for subject, user in [("sender-1", user1), ("sender-2", user2)]:
            bound = await bind_authenticated_self_channel_principal(db, tenant_id=seed["tenant_id"], config=config,
                provider_subject_id=subject, user_id=user.id, actor_user_id=user.id)
            actors.append(bound.actor)
        permission.actions = ["read", "execute"]
        group = await db.get(ChatSession, seed["session_id"])
        group.user_id = group.external_principal_id = None
        group.audience_kind, group.visibility_scope = "group", "group"
        group.group_installation_ref, group.group_subject_id = str(config.id), "oc_shared"
        await db.commit()

    # This existing consumer is RED on the untouched parent: all group sources were unavailable.
    from app.services.content_input_receipts import _authorize_session_input_source
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        group = await db.get(ChatSession, seed["session_id"])
        await _authorize_session_input_source(db, seed["tenant_id"], group, user=user2, runtime_request=None)

    from app.services.channel_session import find_or_create_shared_group_session
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        created = await find_or_create_shared_group_session(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            installation_ref=str(seed["config_id"]), chat_id="oc_native_shared", first_message_title="Shared work")
        shared_id = created.id
        reused = await find_or_create_shared_group_session(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            installation_ref=str(seed["config_id"]), chat_id="oc_native_shared", first_message_title="Do not replace the title")
        other_install = await find_or_create_shared_group_session(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            installation_ref=str(uuid4()), chat_id="oc_native_shared", first_message_title="Other installation")
        assert reused.id == shared_id and reused.title == "Shared work" and other_install.id != shared_id
        assert created.external_conv_id.startswith("feishu_group_shared_") and len(created.external_conv_id) < 200
        assert created.delivery_target_json["receive_id"] == "oc_native_shared" and "external_principal_id" not in created.delivery_target_json
        await db.commit()
    commands = []
    for index, actor in enumerate(actors):
        event_body = {"event": {"sender": {"sender_id": {"open_id": f"sender-{index+1}"}}, "message": {
            "chat_type": "group", "chat_id": "oc_native_shared", "message_type": "text", "content": "shared input"}}}
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            receipt = await accept_authenticated_channel_event(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                provider="feishu", installation_ref=str(seed["config_id"]), provider_event_id=f"group-input-{index}",
                handler_key="feishu.event", body=event_body)
        claimed = await ChannelIngressInboxService(session_factory=app_user_sessionmaker).claim_batch(worker_id="group-input")
        assert any(item.id == receipt.event_id for item in claimed)
        with use_channel_ingress_context(event_id=receipt.event_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            provider="feishu", installation_ref=str(seed["config_id"])):
            bind_channel_ingress_external_principal(actor.external_principal_id)
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                group = await db.get(ChatSession, shared_id)
                db.add(ChatMessage(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=actor.id,
                    external_principal_id=actor.external_principal_id, conversation_id=str(shared_id), role="user", content=f"Input {index}"))
                await db.flush()
                authority = await resolve_session_mutation_authority(db, user=actor, agent_id=seed["agent_id"],
                    session_id=shared_id, action="mutate_session_input")
                assert authority.authority_source == "shared_group_ingress"
                result = await accept_human_input(db, authority=authority, intent={"kind": "start_turn",
                    "session_id": str(shared_id), "input_id": str(receipt.event_id), "idempotency_key": "same-client-key",
                    "content_parts": [{"type": "text", "text": f"Input {index}"}]})
                replay = await accept_human_input(db, authority=authority, intent={"kind": "start_turn",
                    "session_id": str(shared_id), "input_id": str(receipt.event_id), "idempotency_key": "same-client-key",
                    "content_parts": [{"type": "text", "text": f"Input {index}"}]})
                assert replay.replayed is True
                command = await db.get(SessionCommand, result.command_id)
                assert command.principal_type == "external_principal" and command.principal_id == actor.external_principal_id
                commands.append(command.id)
                event = await db.get(ChannelIngressEvent, receipt.event_id)
                event.status, event.external_principal_id = "processed", actor.external_principal_id
                await db.commit()
    assert len(set(commands)) == 2
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        group = await db.get(ChatSession, shared_id)
        assert group.user_id is None and group.external_principal_id is None and group.visibility_scope == "group"
        with pytest.raises(PermissionError, match="shared_group_ingress_required"):
            await resolve_session_mutation_authority(db, user=actors[0], agent_id=seed["agent_id"], session_id=shared_id,
                action="mutate_session_input")
        rows = list(await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == str(shared_id))))
        assert {row.user_id for row in rows} == {user1.id, user2.id} and all(row.source_ingress_event_id for row in rows)
        dependencies = []
        for command_id, actor in zip(commands, actors):
            command = await db.get(SessionCommand, command_id)
            rebuilt = await resolve_session_command_authority(db, command=command, session=group, action="mutate_session_input")
            assert rebuilt.actor.id == actor.id and rebuilt.authority.principal_id == actor.external_principal_id
            dependencies.append({"source_kind": "session_command", "source_id": str(command.id), "source_version": command.request_hash})
        from app.services.session_model_inputs import _hash

        private_control = {"feedback": "PRIVATE_CONTROL_CANARY"}
        control = SessionCommand(id=uuid4(), tenant_id=seed["tenant_id"], session_id=shared_id,
            principal_type="user", principal_id=user1.id, namespace="evaluation_feedback", idempotency_key="private-evaluation",
            command_kind="record_feedback", request_json=private_control, target_json={},
            request_hash=_hash({"command_kind": "record_feedback", "request": private_control}), target_hash=_hash({}),
            status="accepted", receipt_ref="synthetic-private-evaluation")
        db.add(control)
        await db.flush()
        with pytest.raises(ContentInputReceiptError) as derived:
            await _validate_input_dependencies(db, seed["tenant_id"], [{"source_kind": "session_command",
                "source_id": str(control.id), "source_version": control.request_hash}], user=user2)
        assert derived.value.status_code == 503 and "PRIVATE_CONTROL_CANARY" not in str(derived.value)
        with pytest.raises(PermissionError, match="shared_group_input_action_required"):
            await resolve_session_command_authority(db, command=await db.get(SessionCommand, commands[0]),
                session=group, action="mutate_session_control")
        live_permission = await db.get(ResourcePermission, permission.id)
        live_permission.actions = ["read"]
        execute_deny = ResourcePermission(tenant_id=seed["tenant_id"], principal_type="user", principal_id=user2.id,
            resource_type="agent", resource_id=seed["agent_id"], actions=["execute"], effect="deny")
        db.add(execute_deny)
        await db.flush()
        with pytest.raises(HTTPException) as read_only:
            await resolve_session_command_authority(db, command=await db.get(SessionCommand, commands[1]),
                session=group, action="mutate_session_input")
        assert read_only.value.status_code == 403
        live_permission.actions = ["read", "execute"]
        await db.delete(execute_deny)
        await db.flush()
        await _validate_input_dependencies(db, seed["tenant_id"], dependencies, user=user2)
        members.remove("sender-2")
        with pytest.raises(ContentInputReceiptError) as left:
            await _validate_input_dependencies(db, seed["tenant_id"], dependencies, user=user2)
        assert left.value.status_code == 403
        async def unavailable(*_args, **_kwargs):
            raise RuntimeError("synthetic provider unavailable")
        monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", unavailable)
        with pytest.raises(ContentInputReceiptError) as offline:
            await _validate_input_dependencies(db, seed["tenant_id"], dependencies, user=user1)
        assert offline.value.status_code == 503 and offline.value.code == "group_membership_unavailable"
        principal = await db.get(ExternalPrincipal, actors[0].external_principal_id)
        principal.linked_user_id = None
        await db.flush()
        with pytest.raises(PermissionError, match="shared_group_ingress_mismatch"):
            await resolve_session_command_authority(db, command=await db.get(SessionCommand, commands[0]),
                session=group, action="mutate_session_input")
