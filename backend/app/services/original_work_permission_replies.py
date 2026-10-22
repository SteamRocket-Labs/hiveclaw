"""Apply an authenticated original-work quote to its exact native permission."""

import re
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.session_user_question import question_payload


async def resolve_original_work_permission_reply(db, *, notice, run, session, user, principal, ingress, text,
    uploads=(), expected_upload_keys=()):
    from app.api.chat_sessions import ResolveSessionPermissionIn, resolve_session_permission
    from app.core.policy import write_audit_event
    from app.services.channel_agent_runtime import _PERMISSION_ID_PATTERN, _parse_channel_permission_action
    from app.services.channel_delivery_outbox import _claimed
    from app.services.channel_ingress_context import bind_channel_ingress_runtime_result
    from app.services.original_work_permission_notices import (
        _original_work, authorize_original_work_permission_notice, permission_notice_text, permission_notice_quote_enabled,
    )
    from app.services.session_model_inputs import _origin_dependencies

    action = _parse_channel_permission_action(text)
    if action is None or uploads or expected_upload_keys:
        raise HTTPException(400, detail={'code': 'original_permission_command_required'})
    parts = str(text).strip().rstrip('。.!！').rsplit(maxsplit=1)
    if len(parts) == 2 and re.fullmatch(_PERMISSION_ID_PATTERN, parts[-1]) and uuid.UUID(parts[-1]) != uuid.UUID(notice.prompt_key):
        raise HTTPException(409, detail={'code': 'original_permission_request_id_mismatch'})
    try:
        work = await _original_work(db, run)
        invocation = await db.get(SessionToolInvocation, uuid.UUID(notice.metadata_json['invocation_id']), populate_existing=True)
        model = await db.get(SessionModelResult, uuid.UUID(notice.metadata_json['model_result_id']))
        if (work is None or invocation is None or model is None
            or notice.metadata_json.get('source') != 'original_work_permission'
            or notice.metadata_json.get('schema') != 'hive.original_work_permission_notice.v1'
            or notice.metadata_json.get('projection_kind') != 'tool_permission'
            or notice.terminal_status != 'awaiting_permission' or notice.delivery_kind != 'interactive_prompt'
            or notice.artifact_ids_json or notice.metadata_json.get('permission_request_id') != notice.prompt_key
            or invocation.tenant_id != run.tenant_id or invocation.run_id != run.id
            or invocation.session_id != uuid.UUID(run.child_session_id)
            or str(invocation.permission_item_id) != notice.prompt_key
            or model.tenant_id != run.tenant_id or model.run_id != run.id or model.session_id != invocation.session_id
            or model.state != 'round_committed' or model.round_id != invocation.round_id
            or model.provider_request_id != invocation.provider_request_id):
            raise ContentInputReceiptError('original_permission_binding_changed', 'Restore the native permission reference.', 403)
        _original_session, record, binding, _target = work
        if any(notice.metadata_json.get(key) != value for key, value in binding.items()):
            raise ContentInputReceiptError('original_permission_binding_changed', 'Restore the original work binding.', 403)
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == run.tenant_id, ChatTranscriptEvent.session_id == invocation.session_id,
            ChatTranscriptEvent.run_id == run.id, ChatTranscriptEvent.result_id == model.id,
            ChatTranscriptEvent.invocation_id == invocation.id, ChatTranscriptEvent.item_id == invocation.permission_item_id,
            ChatTranscriptEvent.item_kind == 'tool_permission', ChatTranscriptEvent.lifecycle == 'waiting')
            .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        request = question_payload(event).get('permission_request') if event else None
        _sources, complete = await _origin_dependencies(db, tenant_id=run.tenant_id, agent_id=run.child_agent_id,
            ref=f'session-tool-permission:{invocation.id}')
        if (not complete or not isinstance(request, dict) or request.get('requested_by_user_id') != str(user.id)
            or notice.text_content != permission_notice_text(run, record, request,
                quoted_reply=permission_notice_quote_enabled(notice.metadata_json, notice.channel))):
            raise ContentInputReceiptError('original_permission_notice_changed', 'Restore the original permission notice.', 403)
        settled = invocation.permission_state in {'approved', 'denied', 'expired'} and (
            invocation.result_event_id is not None or invocation.effect_state in {'effect_started', 'effect_committed', 'needs_reconciliation'})
        if not settled:
            # A new or still-unstarted effect retains the current full source
            # check. A committed/uncertain effect only replays its native receipt.
            await authorize_original_work_permission_notice(db, item=_claimed(notice), session=session, user=user)
        receipt = await resolve_session_permission(agent_id=run.child_agent_id, session_id=invocation.session_id,
            permission_request_id=invocation.permission_item_id, body=ResolveSessionPermissionIn(action=action),
            current_user=user, db=db)
    except ContentInputReceiptError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code}) from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(403, detail={'code': 'original_permission_binding_changed'}) from exc
    from app.database import pin_rls_tenant_context

    await pin_rls_tenant_context(db, run.tenant_id)
    audit = await write_audit_event(db, event_type='original_work.permission_response_via_channel', actor_type='user',
        actor_id=user.id, tenant_id=run.tenant_id, action='respond_original_work_permission',
        resource_type='runtime_task', resource_id=run.id, request_id=ingress.event_id,
        details={'external_principal_id': str(principal.id), 'channel_ingress_event_id': str(ingress.event_id),
            'original_notice_id': str(notice.id), 'permission_receipt': receipt})
    from app.services.original_work_permission_responses import enqueue_original_work_permission_response

    await enqueue_original_work_permission_response(db, notice=notice, run=run, record=record, receipt=receipt,
        decision=action, audit_event_id=audit.event_id)
    await db.commit()
    bind_channel_ingress_runtime_result(runtime_task_id=run.id, session_id=notice.session_id)
    return {'code': 0, 'msg': 'permission_received', 'permission_status': receipt['status'], 'permission_receipt': receipt}
