"""Nonterminal business projections of native tool permission obligations."""

from sqlalchemy import select

from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.business_task_runtime import BusinessTaskInvariantError, _runtime_binding_matches


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
        return False
    metadata.update(phase='awaiting_permission', interactive_pause='awaiting_session_permission',
        business_wait={'schema': 'hive.business_task_wait.v1', 'session_id': str(session.id),
            'provider_request_id': model.provider_request_id,
            'permission_ids': [str(row.permission_item_id) for row in pending]})
    run.metadata_json, run.status, run.result_summary = metadata, 'suspended', 'awaiting_session_permission'
    run.claimed_by = run.claim_expires_at = run.completed_at = None
    task.status, task.last_execution_status = 'doing', 'awaiting_permission'
    task.completed_at = task.last_error = task.last_result = None
    return True


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
