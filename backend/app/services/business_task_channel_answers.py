"""Relay an authenticated provider quote into its original native question only."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask


async def try_answer_business_question(db, *, agent, principal, config, user_text, delivery_target,
    external_conv_id, uploads=(), expected_upload_keys=()):
    from app.services.channel_ingress_context import (
        bind_channel_ingress_external_principal, bind_channel_ingress_runtime_result, current_channel_ingress_context,
    )
    from app.services.channel_ingress_inbox import channel_installation_ref

    ingress = current_channel_ingress_context()
    if ingress is None or ingress.provider != 'feishu':
        return None
    event = await db.get(ChannelIngressEvent, ingress.event_id, populate_existing=True)
    installation = channel_installation_ref(config, fallback='')
    if (event is None or event.status != 'processing' or event.handler_key != 'feishu.event'
        or event.provider != 'feishu' or event.agent_id != agent.id or event.tenant_id != agent.tenant_id
        or ingress.agent_id != agent.id or ingress.tenant_id != agent.tenant_id
        or event.installation_ref != installation or ingress.installation_ref != installation):
        raise HTTPException(403, detail={'code': 'business_question_authenticated_ingress_required'})
    body = (event.payload_json or {}).get('body') or event.payload_json or {}
    message = (body.get('event') or {}).get('message') or {}
    parent = message.get('parent_id')
    if not isinstance(parent, str) or not parent:
        return None
    candidates = list(await db.scalars(select(ChannelDeliveryOutbox).where(
        ChannelDeliveryOutbox.tenant_id == agent.tenant_id, ChannelDeliveryOutbox.agent_id == agent.id,
        ChannelDeliveryOutbox.channel == 'feishu', ChannelDeliveryOutbox.channel_config_id == config.id,
        ChannelDeliveryOutbox.delivery_kind == 'interactive_prompt',
        ChannelDeliveryOutbox.metadata_json['source'].astext == 'business_task_runtime',
        ChannelDeliveryOutbox.metadata_json['projection_kind'].astext == 'question_notice',
        ChannelDeliveryOutbox.delivery_receipts_json['text']['state'].astext == 'delivered',
        ChannelDeliveryOutbox.delivery_receipts_json['text']['detail']['provider_message_id'].astext == parent).limit(2)))
    if not candidates:
        if principal.linked_user_id is not None:
            unknown = await db.scalar(select(ChannelDeliveryOutbox.id).join(RuntimeTask,
                RuntimeTask.id == ChannelDeliveryOutbox.runtime_task_id).where(
                ChannelDeliveryOutbox.tenant_id == agent.tenant_id, ChannelDeliveryOutbox.agent_id == agent.id,
                ChannelDeliveryOutbox.channel == 'feishu', ChannelDeliveryOutbox.channel_config_id == config.id,
                ChannelDeliveryOutbox.user_id == principal.linked_user_id,
                ChannelDeliveryOutbox.external_principal_id == principal.id,
                ChannelDeliveryOutbox.metadata_json['source'].astext == 'business_task_runtime',
                ChannelDeliveryOutbox.metadata_json['projection_kind'].astext == 'question_notice',
                ChannelDeliveryOutbox.delivery_receipts_json['text']['state'].astext == 'delivered',
                ChannelDeliveryOutbox.delivery_receipts_json['text']['detail']['provider_message_id'].astext.is_(None),
                RuntimeTask.tenant_id == agent.tenant_id, RuntimeTask.task_type == 'business_task',
                RuntimeTask.status == 'suspended',
                RuntimeTask.metadata_json['business_wait']['question_item_id'].astext == ChannelDeliveryOutbox.prompt_key).limit(1))
            if unknown is not None:
                return {'code': 0, 'msg': 'answer_rejected', 'answer_status': 'needs_reconciliation',
                    'reason_code': 'business_question_provider_reference_unavailable'}
        return None
    if len(candidates) != 1:
        return {'code': 0, 'msg': 'answer_rejected', 'answer_status': 'rejected',
            'reason_code': 'business_question_reply_ambiguous'}
    notice = candidates[0]
    try:
        from app.api.tasks import TaskAnswerIn, answer_business_task_question
        from app.core.policy import write_audit_event
        from app.services.channel_provider_receipts import feishu_message_scope
        from app.services.external_principal_service import load_external_runtime_actor
        from app.services.runtime_user_identity import load_scoped_runtime_user

        actor = await load_external_runtime_actor(db, tenant_id=agent.tenant_id,
            principal_id=principal.id, expected_user_id=notice.user_id)
        sender = ((body.get('event') or {}).get('sender') or {}).get('sender_id') or {}
        if principal.installation_ref == installation and principal.subject_id == sender.get('open_id'):
            bind_channel_ingress_external_principal(principal.id)
        target = notice.delivery_target_json or {}
        run = await db.get(RuntimeTask, notice.runtime_task_id, populate_existing=True)
        session = await db.get(ChatSession, notice.session_id, populate_existing=True)
        if (not actor.authority_bound or actor.id is None or actor.id != notice.user_id
            or principal.installation_ref != installation or principal.subject_id != sender.get('open_id')
            or run is None or run.task_type != 'business_task' or run.tenant_id != agent.tenant_id
            or run.parent_agent_id != agent.id or run.root_user_id != actor.id
            or run.parent_session_id != str(notice.session_id) or session is None
            or session.tenant_id != agent.tenant_id or session.agent_id != agent.id):
            raise HTTPException(403, detail={'code': 'business_question_reply_requester_changed'})
        if session.audience_kind == 'group':
            from app.services.shared_group_history import authorize_group_history_reader

            if (message.get('chat_type') != 'group' or target.get('receive_id_type') != 'chat_id'
                or target.get('receive_id') != message.get('chat_id') or session.group_subject_id != message.get('chat_id')):
                raise HTTPException(403, detail={'code': 'business_question_reply_target_changed'})
        elif (notice.external_principal_id != principal.id or session.user_id != actor.id
            or message.get('chat_type') == 'group' or target.get('receive_id_type') != 'open_id'
            or target.get('receive_id') != principal.subject_id):
            raise HTTPException(403, detail={'code': 'business_question_reply_target_changed'})
        scope = ((notice.delivery_receipts_json or {}).get('text', {}).get('detail') or {}).get('provider_message_scope')
        if scope is None:
            return {'code': 0, 'msg': 'answer_rejected', 'answer_status': 'needs_reconciliation',
                'reason_code': 'business_question_provider_reference_unavailable'}
        if scope != feishu_message_scope(config):
            raise HTTPException(403, detail={'code': 'business_question_provider_scope_changed'})
        user = await load_scoped_runtime_user(db, actor.id, tenant_id=agent.tenant_id)
        if user is None:
            raise HTTPException(403, detail={'code': 'business_question_reply_requester_changed'})
        if session.audience_kind == 'group':
            await authorize_group_history_reader(db, session=session, user=user, audit=False)
        bind_channel_ingress_external_principal(principal.id)
        parts = [{'type': 'text', 'text': user_text}]
        if list(expected_upload_keys) != [key for key, _content in uploads]:
            raise HTTPException(503, detail={'code': 'business_question_reply_attachments_unavailable'})
        if uploads:
            from app.services.channel_session import find_or_create_channel_session
            from app.services.feishu_uploads import save_feishu_upload
            from app.services.session_live_input import content_parts_from_live_ingress

            origin = session if session.audience_kind == 'group' else await find_or_create_channel_session(
                db=db, agent_id=agent.id, tenant_id=agent.tenant_id, user_id=actor.id,
                external_principal_id=principal.id, external_conv_id=external_conv_id, source_channel='feishu',
                first_message_title=user_text, delivery_target=delivery_target)
            paths = [await save_feishu_upload(db, actor=actor, session=origin, message_id=message.get('message_id'),
                resource_key=key, filename=f'image_{key[-8:]}.jpg', content=content) for key, content in uploads]
            parts = content_parts_from_live_ingress(content=user_text, attachments=[{'path': path} for path in paths])
        await write_audit_event(db, event_type='business_task.question_answer_via_channel', actor_type='user',
            actor_id=user.id, tenant_id=agent.tenant_id, action='answer_business_task_question',
            resource_type='task', resource_id=uuid.UUID(notice.metadata_json['business_task_id']),
            details={'external_principal_id': str(principal.id), 'channel_ingress_event_id': str(ingress.event_id),
                'question_item_id': notice.metadata_json['question_item_id']})
        receipt = await answer_business_task_question(agent_id=agent.id,
            task_id=uuid.UUID(notice.metadata_json['business_task_id']), current_user=user, db=db,
            data=TaskAnswerIn(input_id=ingress.event_id, question_item_id=uuid.UUID(notice.metadata_json['question_item_id']),
                content_parts=parts))
        bind_channel_ingress_runtime_result(runtime_task_id=run.id, session_id=notice.session_id)
        return {'code': 0, 'msg': 'answer_received', 'answer_status': receipt['status'],
            'reason_code': receipt.get('reason_code')}
    except HTTPException as exc:
        await db.rollback()
        if exc.status_code >= 500:
            raise
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return {'code': 0, 'msg': 'answer_rejected', 'answer_status': 'rejected',
            'reason_code': detail.get('code', 'business_question_reply_unavailable')}
