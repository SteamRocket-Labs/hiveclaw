"""Project canonical native tool permissions for their original work consumers."""

from sqlalchemy import func, or_, select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.session_model_inputs import _origin_dependencies
from app.services.session_user_question import question_payload


async def read_session_run_permissions(db, *, run, session, user):
    if (run.status not in {'running', 'suspended'} or session.tenant_id != run.tenant_id
        or session.runtime_task_id != run.id):
        return []
    rows = (await db.execute(select(ChatTranscriptEvent, SessionToolInvocation).join(SessionToolInvocation,
        SessionToolInvocation.id == ChatTranscriptEvent.invocation_id).join(SessionModelResult,
        SessionModelResult.id == ChatTranscriptEvent.result_id).where(
        ChatTranscriptEvent.tenant_id == run.tenant_id, ChatTranscriptEvent.agent_id == session.agent_id,
        ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.run_id == run.id,
        ChatTranscriptEvent.item_kind == 'tool_permission', ChatTranscriptEvent.item_id == SessionToolInvocation.permission_item_id,
        SessionToolInvocation.tenant_id == run.tenant_id, SessionToolInvocation.session_id == session.id,
        SessionToolInvocation.run_id == run.id, SessionToolInvocation.permission_state == 'waiting',
        SessionToolInvocation.effect_state == 'prepared_not_started', SessionToolInvocation.result_event_id.is_(None),
        or_(SessionToolInvocation.permission_expires_at.is_(None), SessionToolInvocation.permission_expires_at > func.now()),
        SessionModelResult.tenant_id == run.tenant_id, SessionModelResult.session_id == session.id,
        SessionModelResult.run_id == run.id, SessionModelResult.state == 'round_committed',
        SessionToolInvocation.provider_request_id == SessionModelResult.provider_request_id)
        .order_by(ChatTranscriptEvent.sequence))).all()
    can_resolve = user.id is not None and run.root_user_id == user.id
    if can_resolve:
        from fastapi import HTTPException
        from app.core.permissions import authorize_session_action

        try:
            await authorize_session_action(db, user, agent_id=session.agent_id, session_id=session.id,
                action='respond_tool_permission', require_writable=True, audit=False)
        except HTTPException as exc:
            if exc.status_code not in {403, 404, 410}:
                raise
            can_resolve = False
    latest = {event.item_id: (event, invocation) for event, invocation in rows}
    permissions = []
    for event, invocation in latest.values():
        request = question_payload(event).get('permission_request') or {}
        if (event.lifecycle != 'waiting' or request.get('permission_request_id') != str(event.item_id)
            or request.get('session_id') != str(session.id) or request.get('runtime_task_id') != str(run.id)
            or request.get('tool_name') != invocation.tool_name
            or request.get('requested_by_user_id') != str(run.root_user_id)
            or request.get('arguments') != (invocation.effective_arguments_json or invocation.provider_arguments_json or {})):
            continue
        dependencies, complete = await _origin_dependencies(db, tenant_id=run.tenant_id,
            agent_id=session.agent_id, ref=f'session-tool-permission:{invocation.id}')
        if not complete:
            raise ContentInputReceiptError('tool_permission_unverified', 'Restore the committed tool input inventory.', 503)
        await _validate_input_dependencies(db, run.tenant_id, dependencies, user=user)
        permissions.append({'session_id': str(session.id), 'request': request,
            'can_resolve': can_resolve})
    return permissions
