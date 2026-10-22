"""Source-checked question projections addressed to the original business work."""

import uuid

from sqlalchemy import select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.content_input_receipts import ContentInputReceiptError


async def _waiting_question(db, run, question_id):
    from app.models.task import Task
    from app.services.business_task_artifacts import _execution_session
    from app.services.business_task_runtime import _runtime_binding_matches

    session, _root = await _execution_session(db, run)
    try:
        business_id = uuid.UUID((run.metadata_json or {})['business_task_id'])
        question_id = uuid.UUID(str(question_id))
    except (KeyError, ValueError, TypeError) as exc:
        raise ContentInputReceiptError('business_question_binding_unavailable', 'Restore the original task reference.', 503) from exc
    business = await db.get(Task, business_id, populate_existing=True)
    wait = (run.metadata_json or {}).get('business_wait') or {}
    if (business is None or not _runtime_binding_matches(business, run) or run.status != 'suspended'
        or business.status != 'doing' or business.last_execution_status != 'awaiting_user'
        or wait.get('question_item_id') != str(question_id)):
        raise ContentInputReceiptError('business_question_no_longer_waiting', 'Check the current task state.', 403)
    row = (await db.execute(select(ChatTranscriptEvent, SessionModelResult).join(SessionModelResult,
        SessionModelResult.id == ChatTranscriptEvent.result_id).join(SessionToolInvocation,
        SessionToolInvocation.id == ChatTranscriptEvent.invocation_id).where(
        ChatTranscriptEvent.tenant_id == run.tenant_id, ChatTranscriptEvent.agent_id == run.parent_agent_id,
        ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.run_id == run.id,
        ChatTranscriptEvent.item_kind == 'user_question', ChatTranscriptEvent.item_id == question_id,
        SessionModelResult.tenant_id == run.tenant_id, SessionModelResult.run_id == run.id,
        SessionModelResult.session_id == session.id, SessionModelResult.state == 'round_committed',
        SessionToolInvocation.tenant_id == run.tenant_id, SessionToolInvocation.run_id == run.id,
        SessionToolInvocation.session_id == session.id, SessionToolInvocation.tool_name == 'ask_user_question',
        SessionToolInvocation.provider_request_id == SessionModelResult.provider_request_id,
        SessionToolInvocation.effect_state == 'effect_committed')
        .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))).first()
    if row is None or row[0].lifecycle != 'waiting':
        raise ContentInputReceiptError('business_question_no_longer_waiting', 'Check the current question state.', 403)
    return business, row[0], row[1]


def _question_text(business, question):
    from app.config import get_settings
    from app.services.session_user_question import question_payload, render_user_questions

    rendered = render_user_questions(question_payload(question))
    lines = [rendered] if rendered else []
    base = (get_settings().PUBLIC_BASE_URL or get_settings().BASE_URL).rstrip('/')
    lines.append(f'查看并答复原工作：{base}/agents/{business.agent_id}?task_id={business.id}#tasks')
    return '\n'.join(lines)


async def enqueue_business_question_notice(db, *, run, question_id):
    from app.services.channel_delivery_outbox import ChannelDeliveryIntent, enqueue_channel_delivery
    from app.services.channel_delivery_service import ChannelDeliveryService

    target = (run.metadata_json or {}).get('delivery_target') or {}
    channel = str(target.get('channel') or '').lower()
    if not channel or channel == 'web':
        return None
    business, question, model = await _waiting_question(db, run, question_id)
    config = await ChannelDeliveryService._load_target_config(db, run.parent_agent_id,
        'microsoft_teams' if channel == 'teams' else channel, target)
    return await enqueue_channel_delivery(db, ChannelDeliveryIntent(tenant_id=run.tenant_id, runtime_task_id=run.id,
        agent_id=run.parent_agent_id, session_id=run.parent_session_id, user_id=run.root_user_id,
        external_principal_id=target.get('external_principal_id'), channel_config_id=getattr(config, 'id', None),
        delivery_target=target, text=_question_text(business, question), terminal_status='suspended',
        delivery_kind='interactive_prompt', prompt_item_id=question.item_id,
        metadata={'source': 'business_task_runtime', 'projection_kind': 'question_notice',
            'business_task_id': str(business.id), 'question_item_id': str(question.item_id), 'model_result_id': str(model.id)}))


async def authorize_business_question_notice(db, *, item, run, session, user):
    from app.core.resource_authority import authorize_resource_action
    from app.core.source_read_purpose import source_recipient_read
    from app.services.content_input_receipts import _validate_input_dependencies
    from app.services.session_model_inputs import _model_dependencies

    business, question, model = await _waiting_question(db, run, item.metadata.get('question_item_id'))
    if (item.delivery_kind != 'interactive_prompt' or item.artifact_ids
        or item.metadata.get('business_task_id') != str(business.id)
        or item.metadata.get('model_result_id') != str(model.id) or item.text != _question_text(business, question)):
        raise ContentInputReceiptError('business_question_delivery_changed', 'The saved question no longer matches.', 403)
    dependencies, complete = _model_dependencies(model)
    if not complete:
        raise ContentInputReceiptError('business_question_sources_unavailable', 'Restore the question source receipt.', 503)
    if user is None:
        raise ContentInputReceiptError('business_question_reader_unavailable', 'Restore the original requester authority.', 403)
    with source_recipient_read(session_id=session.id, user_id=user.id):
        await authorize_resource_action(db, user, agent_id=business.agent_id, resource_kind='task', resource_id=business.id,
            action='read', owner_user_id=business.created_by, root_session_id=business.root_session_id,
            authority_state=business.authority_state)
        await _validate_input_dependencies(db, run.tenant_id, dependencies, user=user)
    if session.audience_kind == 'group':
        from app.services.shared_group_authority import validate_shared_group_delivery

        await validate_shared_group_delivery(db, item=item, session=session)
