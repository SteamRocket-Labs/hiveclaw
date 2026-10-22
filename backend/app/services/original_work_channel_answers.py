"""Relay an authenticated provider quote into its exact original work question."""

import uuid

from fastapi import HTTPException
from sqlalchemy import String, and_, or_, select

from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask


async def try_answer_original_work_question(db, *, agent, principal, config, user_text, delivery_target,
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
        or_(and_(ChannelDeliveryOutbox.metadata_json['source'].astext == 'business_task_runtime',
                ChannelDeliveryOutbox.metadata_json['projection_kind'].astext == 'question_notice'),
            ChannelDeliveryOutbox.metadata_json['source'].astext == 'employee_first_task_question',
            ChannelDeliveryOutbox.metadata_json['source'].astext == 'original_work_permission',
            ChannelDeliveryOutbox.terminal_status == 'awaiting_permission'),
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
                or_(and_(ChannelDeliveryOutbox.metadata_json['source'].astext == 'business_task_runtime',
                        ChannelDeliveryOutbox.metadata_json['projection_kind'].astext == 'question_notice',
                        RuntimeTask.task_type == 'business_task',
                        RuntimeTask.metadata_json['business_wait']['question_item_id'].astext == ChannelDeliveryOutbox.prompt_key),
                    and_(ChannelDeliveryOutbox.metadata_json['source'].astext == 'employee_first_task_question',
                        RuntimeTask.task_type == 'delegation',
                        RuntimeTask.metadata_json['delegation_waiting_question_item_id'].astext == ChannelDeliveryOutbox.prompt_key)),
                ChannelDeliveryOutbox.delivery_receipts_json['text']['state'].astext == 'delivered',
                ChannelDeliveryOutbox.delivery_receipts_json['text']['detail']['provider_message_id'].astext.is_(None),
                RuntimeTask.tenant_id == agent.tenant_id, RuntimeTask.status == 'suspended').limit(1))
            if unknown is not None:
                return {'code': 0, 'msg': 'answer_rejected', 'answer_status': 'needs_reconciliation',
                    'reason_code': 'business_question_provider_reference_unavailable'}
            from app.models.session_v2 import SessionToolInvocation

            permission_unknown = await db.scalar(select(ChannelDeliveryOutbox.id).join(SessionToolInvocation,
                (SessionToolInvocation.tenant_id == ChannelDeliveryOutbox.tenant_id)
                & (SessionToolInvocation.run_id == ChannelDeliveryOutbox.runtime_task_id)
                & (SessionToolInvocation.permission_item_id.cast(String) == ChannelDeliveryOutbox.prompt_key)).where(
                ChannelDeliveryOutbox.tenant_id == agent.tenant_id, ChannelDeliveryOutbox.agent_id == agent.id,
                ChannelDeliveryOutbox.channel == 'feishu', ChannelDeliveryOutbox.channel_config_id == config.id,
                ChannelDeliveryOutbox.delivery_kind == 'interactive_prompt',
                ChannelDeliveryOutbox.metadata_json['source'].astext == 'original_work_permission',
                ChannelDeliveryOutbox.user_id == principal.linked_user_id, ChannelDeliveryOutbox.external_principal_id == principal.id,
                ChannelDeliveryOutbox.delivery_receipts_json['text']['state'].astext == 'delivered',
                ChannelDeliveryOutbox.delivery_receipts_json['text']['detail']['provider_message_id'].astext.is_(None)).limit(1))
            if permission_unknown is not None:
                return {'code': 0, 'msg': 'permission_rejected', 'permission_status': 'needs_reconciliation',
                    '_permission_feedback_notice_id': str(permission_unknown),
                    'reason_code': 'original_permission_provider_reference_unavailable'}
        return None
    if len(candidates) != 1:
        if any(row.metadata_json.get('source') == 'original_work_permission' or row.terminal_status == 'awaiting_permission' for row in candidates):
            return {'code': 0, 'msg': 'permission_rejected', 'permission_status': 'needs_reconciliation',
                'reason_code': 'original_permission_reply_ambiguous'}
        return {'code': 0, 'msg': 'answer_rejected', 'answer_status': 'rejected',
            'reason_code': 'business_question_reply_ambiguous'}
    notice = candidates[0]
    feedback_notice_id = str(notice.id)
    permission_reply = notice.metadata_json.get('source') == 'original_work_permission' or notice.terminal_status == 'awaiting_permission'
    first_task = notice.metadata_json.get('source') == 'employee_first_task_question' or (
        permission_reply and notice.metadata_json.get('work_kind') == 'employee_first_task')
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
            or run is None or run.task_type != ('delegation' if first_task else 'business_task') or run.tenant_id != agent.tenant_id
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
            if permission_reply:
                return {'code': 0, 'msg': 'permission_rejected', 'permission_status': 'needs_reconciliation',
                    '_permission_feedback_notice_id': feedback_notice_id,
                    'reason_code': 'original_permission_provider_reference_unavailable'}
            return {'code': 0, 'msg': 'answer_rejected', 'answer_status': 'needs_reconciliation',
                'reason_code': 'business_question_provider_reference_unavailable'}
        if scope != feishu_message_scope(config):
            raise HTTPException(403, detail={'code': 'business_question_provider_scope_changed'})
        user = await load_scoped_runtime_user(db, actor.id, tenant_id=agent.tenant_id)
        if user is None:
            raise HTTPException(403, detail={'code': 'business_question_reply_requester_changed'})
        if session.audience_kind == 'group':
            await authorize_group_history_reader(db, session=session, user=user, audit=False)
        if first_task and not permission_reply:
            from app.models.hr_creation import HrCreationDraft
            from app.services.hr_creation_service import direct_creation_authorization

            draft = await db.get(HrCreationDraft, uuid.UUID(notice.metadata_json['draft_id']), populate_existing=True)
            evidence = direct_creation_authorization(draft) if draft is not None else None
            if (draft is None or draft.status != 'completed' or not evidence or not evidence.get('first_task')
                or draft.tenant_id != run.tenant_id or draft.hr_agent_id != run.parent_agent_id
                or draft.session_id != session.id or draft.requested_by_user_id != user.id
                or draft.created_agent_id != run.child_agent_id or run.prompt != evidence['first_task']
                or run.id != uuid.uuid5(draft.id, 'explicit-first-task.v1')
                or run.child_session_id != str(uuid.uuid5(run.id, 'session'))):
                raise HTTPException(403, detail={'code': 'first_task_question_binding_changed'})
            from app.services.employee_first_task import load_first_task_work

            work = await load_first_task_work(db, draft=draft, user=user)
            if work is None or work['status'] in {'unavailable', 'unknown', 'needs_reconciliation'}:
                raise HTTPException(403 if work and work['status'] == 'unavailable' else 503,
                    detail={'code': (work or {}).get('reason', 'first_task_question_source_unavailable')})
        bind_channel_ingress_external_principal(principal.id)
        if permission_reply:
            from app.services.original_work_permission_replies import resolve_original_work_permission_reply

            return await resolve_original_work_permission_reply(db, notice=notice, run=run, session=session,
                user=user, principal=principal, ingress=ingress, text=user_text,
                uploads=uploads, expected_upload_keys=expected_upload_keys)
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
        await write_audit_event(db, event_type='employee_first_task.question_answer_via_channel' if first_task else 'business_task.question_answer_via_channel', actor_type='user',
            actor_id=user.id, tenant_id=agent.tenant_id, action='answer_original_work_question',
            resource_type='runtime_task' if first_task else 'task',
            resource_id=run.id if first_task else uuid.UUID(notice.metadata_json['business_task_id']),
            details={'external_principal_id': str(principal.id), 'channel_ingress_event_id': str(ingress.event_id),
                'question_item_id': notice.metadata_json['question_item_id']})
        if first_task:
            from app.api.chat_sessions import SessionHumanInputIn, submit_session_human_input

            receipt = await submit_session_human_input(agent_id=run.child_agent_id,
                session_id=uuid.UUID(run.child_session_id), current_user=user, db=db,
                body=SessionHumanInputIn(kind='answer_request', input_id=ingress.event_id,
                    idempotency_key=str(ingress.event_id), request_item_id=uuid.UUID(notice.metadata_json['question_item_id']),
                    content_parts=parts, expected_run_id=run.id, terminal_fallback='reject'))
        else:
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
        if permission_reply:
            return {'code': 0, 'msg': 'permission_rejected', 'permission_status': 'rejected',
                '_permission_feedback_notice_id': feedback_notice_id,
                'reason_code': detail.get('code', 'original_permission_reply_unavailable')}
        return {'code': 0, 'msg': 'answer_rejected', 'answer_status': 'rejected',
            'reason_code': detail.get('code', 'business_question_reply_unavailable')}
