"""Nonterminal business projections of native permission and question waits."""

from sqlalchemy import select

from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.business_task_runtime import BusinessTaskInvariantError, _runtime_binding_matches


async def lock_business_question_parent(db, *, tenant_id, agent_id, session_id):
    """Take original-work authority before child input and runtime row locks."""
    import uuid
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.services.chat_transcript import lock_transcript_session

    parent = await db.scalar(select(RuntimeTask.parent_session_id).join(ChatSession,
        ChatSession.runtime_task_id == RuntimeTask.id).where(RuntimeTask.tenant_id == tenant_id,
        RuntimeTask.task_type == 'business_task', RuntimeTask.parent_agent_id == agent_id,
        RuntimeTask.child_agent_id == agent_id, RuntimeTask.child_session_id == str(session_id),
        ChatSession.id == session_id, ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id))
    if parent:
        await lock_transcript_session(db, session_id=uuid.UUID(parent))


async def apply_business_task_permission_wait(db, *, task, run, pause):
    from app.services.business_task_artifacts import _execution_session
    from app.services.runtime_task_fence import assert_runtime_task_fence

    if not _runtime_binding_matches(task, run) or run.child_session_id != pause.reflection_session_id:
        raise BusinessTaskInvariantError('business permission wait scope changed')
    session, _root = await _execution_session(db, run)
    model = await db.scalar(select(SessionModelResult).where(SessionModelResult.tenant_id == run.tenant_id,
        SessionModelResult.run_id == run.id, SessionModelResult.session_id == session.id,
        SessionModelResult.provider_request_id == pause.provider_request_id,
        SessionModelResult.state == 'round_committed'))
    if model is None:
        raise BusinessTaskInvariantError('business permission wait has no committed native round')
    metadata = dict(run.metadata_json or {})
    resume = metadata.get('session_permission_resume') or {}
    if run.status == 'resumable' and resume.get('source_result_id') == str(model.id):
        return True  # A response committed before the worker reached its pause boundary.
    assert_runtime_task_fence(run)
    pending = list(await db.scalars(select(SessionToolInvocation).where(
        SessionToolInvocation.tenant_id == run.tenant_id, SessionToolInvocation.run_id == run.id,
        SessionToolInvocation.session_id == session.id, SessionToolInvocation.provider_request_id == model.provider_request_id,
        SessionToolInvocation.permission_state == 'waiting', SessionToolInvocation.effect_state == 'prepared_not_started',
        SessionToolInvocation.result_event_id.is_(None))))
    if not pending or any(row.permission_item_id is None for row in pending):
        return await _apply_question_wait(db, task=task, run=run, session=session, model=model)
    metadata.update(phase='awaiting_permission', interactive_pause='awaiting_session_permission',
        business_wait={'schema': 'hive.business_task_wait.v1', 'session_id': str(session.id),
            'provider_request_id': model.provider_request_id,
            'permission_ids': [str(row.permission_item_id) for row in pending]})
    run.metadata_json, run.status, run.result_summary = metadata, 'suspended', 'awaiting_session_permission'
    run.claimed_by = run.claim_expires_at = run.completed_at = None
    task.status, task.last_execution_status = 'doing', 'awaiting_permission'
    task.completed_at = task.last_error = task.last_result = None
    from app.services.original_work_permission_notices import enqueue_original_work_permission_notices

    await enqueue_original_work_permission_notices(db, run=run, model=model, invocations=pending)
    return True


async def _apply_question_wait(db, *, task, run, session, model):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput

    question = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id == run.tenant_id,
        ChatTranscriptEvent.agent_id == run.parent_agent_id, ChatTranscriptEvent.session_id == session.id,
        ChatTranscriptEvent.run_id == run.id, ChatTranscriptEvent.result_id == model.id,
        ChatTranscriptEvent.item_kind == 'user_question', ChatTranscriptEvent.lifecycle.in_(['waiting', 'completed']))
        .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
    if question is None:
        return False
    answer = await db.scalar(select(SessionTurnInput.id).join(SessionInputAdmission,
        SessionInputAdmission.input_id == SessionTurnInput.id).where(SessionTurnInput.tenant_id == run.tenant_id,
        SessionTurnInput.session_id == session.id, SessionTurnInput.target_run_id == run.id,
        SessionTurnInput.request_item_id == question.item_id, SessionTurnInput.intent == 'answer_request',
        SessionTurnInput.status == 'queued', SessionInputAdmission.input_revision == SessionTurnInput.revision,
        SessionInputAdmission.state == 'admitted'))
    run.status = 'resumable' if answer else 'suspended'
    run.result_summary = 'awaiting_user_clarification' if not answer else None
    run.claimed_by = run.claim_expires_at = run.completed_at = None
    run.metadata_json = {**(run.metadata_json or {}), 'phase': 'invoking' if answer else 'awaiting_user',
        'interactive_pause': None if answer else 'awaiting_user_clarification',
        'business_wait': {'schema': 'hive.business_task_wait.v1', 'kind': 'question',
            'session_id': str(session.id), 'provider_request_id': model.provider_request_id,
            'question_item_id': str(question.item_id)}}
    task.status, task.last_execution_status = 'doing', 'queued' if answer else 'awaiting_user'
    task.last_result = task.last_error = task.completed_at = None
    if not answer:
        from app.services.business_task_question_delivery import enqueue_business_question_notice

        await enqueue_business_question_notice(db, run=run, question_id=question.item_id)
    return True


async def read_business_task_questions(db, task, user):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.services.business_task_artifacts import _execution_session, business_task_output_visibility
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.session_user_question import question_payload

    if task.active_runtime_task_id is None or (await business_task_output_visibility(db, task, user))['status'] != 'available':
        return []
    run = await db.get(RuntimeTask, task.active_runtime_task_id)
    if (run is None or not _runtime_binding_matches(task, run) or run.status not in {'running', 'suspended'}
        or not run.child_session_id):
        return []
    try:
        session, _root = await _execution_session(db, run)
    except ContentInputReceiptError:
        return []
    rows = list(await db.scalars(select(ChatTranscriptEvent).join(SessionModelResult,
        SessionModelResult.id == ChatTranscriptEvent.result_id).join(SessionToolInvocation,
        SessionToolInvocation.id == ChatTranscriptEvent.invocation_id).where(ChatTranscriptEvent.tenant_id == run.tenant_id,
        ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.run_id == run.id,
        SessionModelResult.tenant_id == run.tenant_id, SessionModelResult.run_id == run.id,
        SessionModelResult.session_id == session.id, SessionModelResult.state == 'round_committed',
        SessionToolInvocation.tenant_id == run.tenant_id, SessionToolInvocation.run_id == run.id,
        SessionToolInvocation.session_id == session.id, SessionToolInvocation.tool_name == 'ask_user_question',
        SessionToolInvocation.provider_request_id == SessionModelResult.provider_request_id,
        SessionToolInvocation.effect_state == 'effect_committed',
        ChatTranscriptEvent.item_kind == 'user_question').order_by(ChatTranscriptEvent.sequence)))
    latest = {row.item_id: row for row in rows}
    return [{'question_item_id': str(row.item_id), 'session_id': str(session.id),
        'can_answer': run.root_user_id == user.id, 'payload': question_payload(row)}
        for row in latest.values() if row.lifecycle == 'waiting']


async def read_business_task_permissions(db, task, user):
    """Expose only current native permission requests in the original task view."""
    from app.models.runtime_task import RuntimeTask
    from app.services.business_task_artifacts import _execution_session, business_task_output_visibility
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.session_permission_views import read_session_run_permissions

    if task.active_runtime_task_id is None or (await business_task_output_visibility(db, task, user))['status'] != 'available':
        return []
    run = await db.get(RuntimeTask, task.active_runtime_task_id)
    if (run is None or not _runtime_binding_matches(task, run) or run.status not in {'running', 'suspended'}
        or not run.child_session_id):
        return []
    try:
        session, _root = await _execution_session(db, run)
        return await read_session_run_permissions(db, run=run, session=session, user=user)
    except ContentInputReceiptError:
        return []


async def sync_business_permission_resume(db, run):
    """Keep the business projection linked when its native tool batch resumes."""
    if run.task_type != 'business_task':
        return
    task = await _business_projection(db, run)
    task.status, task.last_execution_status, task.completed_at = 'doing', 'queued', None
    run.metadata_json = {**(run.metadata_json or {}), 'phase': 'invoking'}


async def sync_business_permission_reconciliation(db, run, reason_code):
    if run.task_type != 'business_task':
        return
    from app.services.business_task_runtime import (
        TaskExecutionOutcome, TaskExecutionStatus, apply_business_task_outcome,
        _enqueue_business_task_channel_delivery,
    )

    task = await _business_projection(db, run)
    outcome = TaskExecutionOutcome(status=TaskExecutionStatus.NEEDS_RECONCILIATION,
        summary='The authorized tool effect requires reconciliation before further work.',
        error_code=reason_code, reflection_session_id=run.child_session_id, retryable=False)
    apply_business_task_outcome(db=db, task=task, runtime_task=run, outcome=outcome)
    await _enqueue_business_task_channel_delivery(db=db, runtime_task=run, outcome=outcome)


async def _business_projection(db, run):
    import uuid
    from app.models.task import Task
    from app.services.business_task_artifacts import _execution_session

    task = await db.scalar(select(Task).where(Task.id == uuid.UUID((run.metadata_json or {})['business_task_id']),
        Task.tenant_id == run.tenant_id, Task.agent_id == run.parent_agent_id).with_for_update())
    if task is None or not _runtime_binding_matches(task, run):
        raise BusinessTaskInvariantError('business permission projection scope changed')
    await _execution_session(db, run)
    return task
