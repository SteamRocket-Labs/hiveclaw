"""Read and deliver committed first-task progress to its original work."""

import uuid

from sqlalchemy import select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.services.content_input_receipts import ContentInputReceiptError

SOURCE = 'original_work_progress'
SCHEMA = 'hive.original_work_progress.v1'
STATUS = 'in_progress'
ACTIVE = {'running', 'resumable', 'suspended'}


async def _committed_progress(db, *, run, provider_request_id=None):
    statement = select(SessionModelResult, ChatTranscriptEvent).join(ChatTranscriptEvent,
        ChatTranscriptEvent.id == SessionModelResult.round_committed_event_id).where(
        SessionModelResult.tenant_id == run.tenant_id, SessionModelResult.run_id == run.id,
        SessionModelResult.session_id == uuid.UUID(run.child_session_id), SessionModelResult.state == 'round_committed',
        ChatTranscriptEvent.tenant_id == run.tenant_id, ChatTranscriptEvent.agent_id == run.child_agent_id,
        ChatTranscriptEvent.session_id == SessionModelResult.session_id, ChatTranscriptEvent.run_id == run.id,
        ChatTranscriptEvent.result_id == SessionModelResult.id, ChatTranscriptEvent.item_kind == 'result_commit',
        ChatTranscriptEvent.lifecycle == 'round_committed')
    if provider_request_id is not None:
        statement = statement.where(SessionModelResult.provider_request_id == provider_request_id)
    row = (await db.execute(statement.order_by(ChatTranscriptEvent.sequence.desc()).limit(1)
        .execution_options(populate_existing=True))).first()
    if row is None:
        return None
    model, event = row
    seal = model.seal_json or {}
    if seal.get('logical_round_complete') is not True or not (seal.get('response') or {}).get('tool_calls'):
        return None
    text = seal.get('semantic_content')
    if not isinstance(text, str) or not text.strip():
        return None
    from app.services.session_model_round import _sha256

    sealed = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == run.tenant_id, ChatTranscriptEvent.session_id == model.session_id,
        ChatTranscriptEvent.run_id == run.id, ChatTranscriptEvent.result_id == model.id,
        ChatTranscriptEvent.item_kind == 'result_commit', ChatTranscriptEvent.lifecycle == 'sealed')
        .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
    if (seal.get('result_id') != str(model.id) or seal.get('run_id') != str(run.id)
        or seal.get('round_id') != model.round_id or seal.get('provider_request_id') != model.provider_request_id
        or seal.get('content_hash') != _sha256(text) or sealed is None or sealed.content_hash != seal['content_hash']):
        raise ContentInputReceiptError('progress_model_unverified', 'Restore the committed model progress receipt.', 503)
    return model, event, text


async def read_original_work_progress(db, *, run, user):
    from app.core.permissions import authorize_session_action
    from app.core.source_read_purpose import source_recipient_read
    from app.services.content_input_receipts import _validate_input_dependencies
    from app.services.original_work_permission_notices import _original_work
    from app.services.session_model_inputs import _model_dependencies

    if run.task_type != 'delegation' or run.status not in ACTIVE:
        return None
    work = await _original_work(db, run)
    if work is None:
        return None
    session, _record, _binding, _target = work
    if user is None or user.id != run.root_user_id or session.user_id != user.id:
        raise ContentInputReceiptError('progress_requester_unavailable', 'Restore the original requester authority.', 403)
    await authorize_session_action(db, user, agent_id=run.parent_agent_id, session_id=session.id,
        action='read_content_input_source', audit=False)
    await authorize_session_action(db, user, agent_id=run.child_agent_id, session_id=uuid.UUID(run.child_session_id),
        action='read_content_input_source', audit=False)
    candidate = await _committed_progress(db, run=run)
    if candidate is None:
        return None
    model, event, text = candidate
    dependencies, complete = _model_dependencies(model)
    if not complete:
        raise ContentInputReceiptError('progress_sources_unverified', 'Restore the complete progress source receipt.', 503)
    with source_recipient_read(session_id=session.id, user_id=user.id):
        await _validate_input_dependencies(db, run.tenant_id, dependencies, user=user)
    return {'model_result_id': str(model.id), 'event_id': str(event.id), 'text': text}


def progress_notice_text(run, record, text):
    from app.config import get_settings

    result = '首任务进度更新；此提示不代表任务完成。\n\n' + text
    base = (get_settings().PUBLIC_BASE_URL or get_settings().BASE_URL).rstrip('/')
    if base:
        result += f'\n\n{base}/agents/{run.parent_agent_id}?session_id={record.session_id}#chat'
    return result


async def enqueue_original_work_progress(db, *, run_id, provider_request_id):
    from app.services.channel_delivery_outbox import ChannelDeliveryIntent, enqueue_channel_delivery
    from app.services.channel_delivery_service import ChannelDeliveryService
    from app.services.original_work_permission_notices import _original_work

    run = await db.get(RuntimeTask, run_id)
    if run is None or run.task_type != 'delegation' or run.status not in ACTIVE:
        return
    work = await _original_work(db, run)
    if work is None:
        return
    session, record, binding, target = work
    target = ChannelDeliveryService.normalize_reply_target(target)
    if not target or target['channel'] == 'web':
        return
    candidate = await _committed_progress(db, run=run, provider_request_id=provider_request_id)
    if candidate is None:
        return
    model, event, text = candidate
    config = await ChannelDeliveryService._load_target_config(db, run.parent_agent_id, target['channel'], target)
    await enqueue_channel_delivery(db, ChannelDeliveryIntent(tenant_id=run.tenant_id, runtime_task_id=run.id,
        agent_id=run.parent_agent_id, session_id=session.id, user_id=run.root_user_id,
        external_principal_id=target.get('external_principal_id') or session.external_principal_id,
        channel_config_id=getattr(config, 'id', None), delivery_target=target,
        text=progress_notice_text(run, record, text), terminal_status=STATUS, delivery_kind='interactive_prompt',
        prompt_item_id=model.id, metadata={'source': SOURCE, 'schema': SCHEMA, **binding,
            'model_result_id': str(model.id), 'event_id': str(event.id)}))


async def authorize_original_work_progress(db, *, item, session, user):
    from app.services.original_work_permission_notices import _original_work

    run = await db.get(RuntimeTask, item.runtime_task_id, populate_existing=True)
    if (run is None or run.tenant_id != item.tenant_id or run.task_type != 'delegation'
        or run.parent_agent_id != item.agent_id or run.parent_session_id != str(item.session_id)
        or run.root_user_id != item.user_id or user is None or user.id != item.user_id
        or session is None or session.id != item.session_id or session.tenant_id != item.tenant_id
        or session.agent_id != item.agent_id or item.metadata.get('source') != SOURCE
        or item.metadata.get('schema') != SCHEMA or item.terminal_status != STATUS
        or item.delivery_kind != 'interactive_prompt' or item.artifact_ids):
        raise ContentInputReceiptError('progress_notice_binding_changed', 'Restore the original progress binding.', 403)
    work = await _original_work(db, run)
    progress = await read_original_work_progress(db, run=run, user=user)
    if work is None or progress is None:
        raise ContentInputReceiptError('progress_no_longer_current', 'Read the latest original work.', 403)
    _session, record, binding, target = work
    from app.services.channel_delivery_service import ChannelDeliveryService

    if session.audience_kind != 'group' and item.channel == 'feishu':
        from app.models.external_principal import ExternalPrincipal

        principal = await db.get(ExternalPrincipal, item.external_principal_id) if item.external_principal_id else None
        if (principal is None or principal.tenant_id != item.tenant_id
            or session.external_principal_id not in {None, principal.id}
            or item.delivery_target.get('receive_id_type') != 'open_id'
            or item.delivery_target.get('receive_id') != principal.subject_id):
            raise ContentInputReceiptError('progress_recipient_changed', 'Restore the original channel recipient.', 403)
    if (any(item.metadata.get(key) != value for key, value in binding.items())
        or ChannelDeliveryService.normalize_reply_target(target) != item.delivery_target
        or item.prompt_key != progress['model_result_id'] or item.metadata.get('model_result_id') != progress['model_result_id']
        or item.metadata.get('event_id') != progress['event_id']
        or item.text != progress_notice_text(run, record, progress['text'])):
        raise ContentInputReceiptError('progress_notice_changed', 'Read the latest committed progress.', 403)
