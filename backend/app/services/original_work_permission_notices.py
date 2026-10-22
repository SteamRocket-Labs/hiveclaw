"""Publish native permission waits into their existing original-work destination."""

import json
import logging
import uuid

from sqlalchemy import select

from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.session_user_question import question_payload

logger = logging.getLogger(__name__)
QUOTE_SCHEMA = 'hive.original_work_permission_quote.v1'


def permission_notice_quote_enabled(metadata, channel):
    marker = metadata.get('interaction_schema')
    if marker is None:
        return False
    if marker != QUOTE_SCHEMA or channel != 'feishu':
        raise ContentInputReceiptError('permission_notice_protocol_changed', 'Restore the supported notice protocol.', 403)
    return True


async def _original_work(db, run):
    try:
        original_id = uuid.UUID(str(run.parent_session_id))
    except (ValueError, TypeError) as exc:
        raise ContentInputReceiptError('permission_original_work_unavailable', 'Restore the original work.', 503) from exc
    session = await db.get(ChatSession, original_id, populate_existing=True)
    if (session is None or session.tenant_id != run.tenant_id or session.agent_id != run.parent_agent_id):
        raise ContentInputReceiptError('permission_original_work_unavailable', 'Restore the original work.', 503)
    if run.task_type == 'delegation':
        from app.models.hr_creation import HrCreationDraft
        from app.services.hr_creation_service import direct_creation_authorization

        draft = await db.scalar(select(HrCreationDraft).where(HrCreationDraft.tenant_id == run.tenant_id,
            HrCreationDraft.hr_agent_id == run.parent_agent_id, HrCreationDraft.session_id == session.id,
            HrCreationDraft.requested_by_user_id == run.root_user_id, HrCreationDraft.created_agent_id == run.child_agent_id))
        evidence = direct_creation_authorization(draft) if draft is not None else None
        if draft is None or run.id != uuid.uuid5(draft.id, 'explicit-first-task.v1'):
            return None
        if (draft.status != 'completed' or not evidence or run.prompt != evidence.get('first_task')
            or run.child_session_id != str(uuid.uuid5(run.id, 'session'))):
            raise ContentInputReceiptError('permission_original_work_changed', 'Restore the original first task.', 403)
        return session, draft, {'work_kind': 'employee_first_task', 'draft_id': str(draft.id)}, session.delivery_target_json or {}
    if run.task_type == 'business_task':
        from app.models.task import Task
        from app.services.business_task_runtime import _runtime_binding_matches

        try:
            business = await db.get(Task, uuid.UUID((run.metadata_json or {})['business_task_id']), populate_existing=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ContentInputReceiptError('permission_original_work_unavailable', 'Restore the original assignment.', 503) from exc
        if business is None or not _runtime_binding_matches(business, run):
            raise ContentInputReceiptError('permission_original_work_changed', 'Restore the original assignment binding.', 403)
        return session, business, {'work_kind': 'business_task', 'business_task_id': str(business.id)}, (run.metadata_json or {}).get('delivery_target') or {}
    return None


def permission_notice_text(run, record, request, *, quoted_reply=False):
    from app.config import get_settings

    first_task = run.task_type == 'delegation'
    label = '首任务' if first_task else '这项工作'
    text = f'{label}有一项工具权限待处理；此提示不代表任务完成。\n'
    text += f"操作：{request.get('tool_display_name') or request['tool_name']}\n"
    text += json.dumps(request.get('arguments') or {}, ensure_ascii=False, indent=2)
    if quoted_reply:
        actions = '/allow 或 /deny'
        if (request.get('allow_session_allowed') is not False and request.get('risk_class') != 'destructive_delete'
            and request.get('confirmation_kind') != 'destructive_once'):
            actions = '/allow、/allow-session 或 /deny'
        text += f'\n引用此消息回复 {actions}，即可处理这项权限；也可打开原工作。'
        text += f"\nrequest_id={request['permission_request_id']}"
    else:
        text += '\n请在 HiveClaw 的原工作中查看并处理这项权限。'
    base = (get_settings().PUBLIC_BASE_URL or get_settings().BASE_URL).rstrip('/')
    if base:
        anchor = f'session_id={record.session_id}#chat' if first_task else f'task_id={record.id}#tasks'
        text += f'\n{base}/agents/{run.parent_agent_id}?{anchor}'
    return text


async def enqueue_original_work_permission_notices(db, *, run, model, invocations):
    from app.services.channel_delivery_outbox import ChannelDeliveryIntent, enqueue_channel_delivery
    from app.services.channel_delivery_service import ChannelDeliveryService

    try:
        work = await _original_work(db, run)
    except ContentInputReceiptError as exc:
        # Unavailable notice material does not terminate the native wait.
        logger.warning('Original-work permission notice unavailable: run=%s code=%s', run.id, exc.code)
        return
    if work is None:
        return
    session, record, binding, target = work
    target = ChannelDeliveryService.normalize_reply_target(target)
    if not target or target['channel'] == 'web':
        return
    config = await ChannelDeliveryService._load_target_config(db, run.parent_agent_id, target['channel'], target)
    for invocation in invocations:
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == run.tenant_id, ChatTranscriptEvent.agent_id == run.child_agent_id,
            ChatTranscriptEvent.session_id == invocation.session_id, ChatTranscriptEvent.run_id == run.id,
            ChatTranscriptEvent.result_id == model.id, ChatTranscriptEvent.invocation_id == invocation.id,
            ChatTranscriptEvent.item_id == invocation.permission_item_id,
            ChatTranscriptEvent.item_kind == 'tool_permission', ChatTranscriptEvent.lifecycle == 'waiting')
            .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        request = question_payload(event).get('permission_request') if event is not None else None
        if not isinstance(request, dict):
            logger.warning('Native permission notice material unavailable: run=%s permission=%s', run.id, invocation.permission_item_id)
            continue
        await enqueue_channel_delivery(db, ChannelDeliveryIntent(tenant_id=run.tenant_id, runtime_task_id=run.id,
            agent_id=run.parent_agent_id, session_id=session.id, user_id=run.root_user_id,
            external_principal_id=target.get('external_principal_id') or session.external_principal_id,
            channel_config_id=getattr(config, 'id', None), delivery_target=target,
            text=permission_notice_text(run, record, request, quoted_reply=target['channel'] == 'feishu'), terminal_status='awaiting_permission',
            delivery_kind='interactive_prompt', prompt_item_id=invocation.permission_item_id,
            metadata={'source': 'original_work_permission', 'schema': 'hive.original_work_permission_notice.v1',
                'projection_kind': 'tool_permission', **binding, 'invocation_id': str(invocation.id),
                'model_result_id': str(model.id), 'permission_request_id': str(invocation.permission_item_id),
                **({'interaction_schema': QUOTE_SCHEMA} if target['channel'] == 'feishu' else {})}))


async def authorize_original_work_permission_notice(db, *, item, session, user):
    from app.core.source_read_purpose import source_recipient_read
    from app.services.employee_first_task import load_first_task_work
    from app.services.business_task_waits import read_business_task_permissions

    run = await db.get(RuntimeTask, item.runtime_task_id, populate_existing=True)
    if (run is None or user is None or run.tenant_id != item.tenant_id or run.parent_agent_id != item.agent_id
        or run.parent_session_id != str(item.session_id) or run.root_user_id != user.id or item.user_id != user.id
        or session is None or session.id != item.session_id or session.tenant_id != item.tenant_id
        or session.agent_id != item.agent_id or item.artifact_ids or item.delivery_kind != 'interactive_prompt'
        or item.terminal_status != 'awaiting_permission' or item.metadata.get('source') != 'original_work_permission'
        or item.metadata.get('schema') != 'hive.original_work_permission_notice.v1'
        or item.metadata.get('projection_kind') != 'tool_permission'):
        raise ContentInputReceiptError('permission_notice_binding_changed', 'Restore the original permission notice.', 403)
    work = await _original_work(db, run)
    if work is None:
        raise ContentInputReceiptError('permission_notice_binding_changed', 'Restore the original permission notice.', 403)
    _session, record, binding, _target = work
    if any(item.metadata.get(key) != value for key, value in binding.items()):
        raise ContentInputReceiptError('permission_notice_binding_changed', 'Restore the original permission notice.', 403)
    if session.audience_kind != 'group' and item.channel == 'feishu':
        from app.models.external_principal import ExternalPrincipal

        principal = await db.get(ExternalPrincipal, item.external_principal_id) if item.external_principal_id else None
        if (principal is None or principal.tenant_id != item.tenant_id
            or session.external_principal_id not in {None, principal.id}
            or item.delivery_target.get('receive_id_type') != 'open_id'
            or item.delivery_target.get('receive_id') != principal.subject_id):
            raise ContentInputReceiptError('permission_notice_recipient_changed', 'Restore the original channel recipient.', 403)
    with source_recipient_read(session_id=session.id, user_id=user.id):
        if binding['work_kind'] == 'employee_first_task':
            current = await load_first_task_work(db, draft=record, user=user)
            if current is None or current['status'] in {'unavailable', 'unknown', 'needs_reconciliation'}:
                raise ContentInputReceiptError((current or {}).get('reason', 'permission_notice_source_unavailable'),
                    'Restore the original permission sources.', 503 if current and current['status'] == 'unknown' else 403)
            permissions = current['permissions']
        else:
            permissions = await read_business_task_permissions(db, record, user)
    request = next((p['request'] for p in permissions if p['request']['permission_request_id'] == item.prompt_key), None)
    if (request is None or item.metadata.get('permission_request_id') != item.prompt_key
        or item.text != permission_notice_text(run, record, request,
            quoted_reply=permission_notice_quote_enabled(item.metadata, item.channel))):
        raise ContentInputReceiptError('permission_notice_no_longer_current', 'Check the current original work.', 403)
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation

    receipt = (await db.execute(select(SessionToolInvocation.id, SessionModelResult.id).join(SessionModelResult,
        (SessionModelResult.tenant_id == SessionToolInvocation.tenant_id)
        & (SessionModelResult.run_id == SessionToolInvocation.run_id)
        & (SessionModelResult.session_id == SessionToolInvocation.session_id)
        & (SessionModelResult.round_id == SessionToolInvocation.round_id)
        & (SessionModelResult.provider_request_id == SessionToolInvocation.provider_request_id)).where(
        SessionToolInvocation.tenant_id == run.tenant_id, SessionToolInvocation.run_id == run.id,
        SessionToolInvocation.session_id == uuid.UUID(run.child_session_id),
        SessionToolInvocation.permission_item_id == uuid.UUID(item.prompt_key),
        SessionModelResult.state == 'round_committed'))).one_or_none()
    if (receipt is None or item.metadata.get('invocation_id') != str(receipt[0])
        or item.metadata.get('model_result_id') != str(receipt[1])):
        raise ContentInputReceiptError('permission_notice_receipt_changed', 'Restore the committed permission receipt.', 403)
