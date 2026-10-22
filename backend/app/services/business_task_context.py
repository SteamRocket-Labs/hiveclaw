"""Compiler provenance for a business task's exact requester-owned input."""

from copy import deepcopy
from datetime import UTC, datetime
import uuid

from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.runtime.context_candidates import ContextMessageOrigin
from app.services.session_model_inputs import _hash


async def _facts(db, run, *, require_active=True):
    if run is None or run.task_type != 'business_task' or run.root_user_id is None:
        return None
    try:
        task = await db.get(Task, uuid.UUID((run.metadata_json or {})['business_task_id']), populate_existing=True)
        session = await db.get(ChatSession, uuid.UUID(run.child_session_id), populate_existing=True)
    except (ValueError, TypeError, KeyError):
        return None
    if (task is None or session is None or task.tenant_id != run.tenant_id or task.agent_id != run.parent_agent_id
        or (require_active and task.active_runtime_task_id != run.id) or session.tenant_id != run.tenant_id
        or session.agent_id != run.parent_agent_id or session.user_id != run.root_user_id
        or session.runtime_task_id != run.id or session.session_kind != 'user_task'):
        return None
    return {'task_id': str(task.id), 'run_id': str(run.id), 'tenant_id': str(run.tenant_id),
        'agent_id': str(run.parent_agent_id), 'session_id': str(session.id), 'user_id': str(run.root_user_id),
        'root_session_id': run.parent_session_id, 'title': task.title, 'description': task.description or '', 'type': task.type,
        'created_by': str(task.created_by), 'request_hash': task.request_hash,
        'plan_id': str(task.plan_id) if task.plan_id else None, 'plan_version': task.plan_version, 'plan_hash': task.plan_hash}


async def bind_business_task_context(db, *, run, invocation):
    from app.runtime.context import ensure_runtime_assembly_state
    from app.services.delegation_context_sources import PermissionContextSource, _permission_inputs
    from app.services.task_executor import _build_task_user_prompt

    facts = await _facts(db, run)
    if (facts is None or invocation.agent_id != run.parent_agent_id or invocation.user_id != run.root_user_id
        or invocation.session_context.session_id != facts['session_id']
        or invocation.messages != [{'role': 'user', 'content': _build_task_user_prompt(facts['title'], facts['description'])}]):
        raise RuntimeError('business_task_input_binding_unavailable')
    proof = {'schema': 'hive.business_task_context.v1', 'task': facts,
        'message': deepcopy(invocation.messages[0]), 'framing': invocation.system_prompt_suffix,
        'permission_inputs': _permission_inputs(invocation.session_context.metadata),
        'allowed_tool_names': list(invocation.allowed_tool_names)}
    digest = _hash(proof)
    source_id = uuid.uuid5(run.id, f'business-task-context:{digest}')
    row = await db.get(InvocationSpan, source_id)
    if row is None:
        db.add(InvocationSpan(id=source_id, tenant_id=run.tenant_id, agent_id=run.parent_agent_id,
            user_id=run.root_user_id, session_id=facts['session_id'], runtime_task_id=run.id,
            trace_id=f'session-run:{run.id}', span_id=f'business-task-context:{source_id}',
            span_type='business_task_context', name='requester_task_input', status='captured', duration_ms=0,
            started_at=datetime.now(UTC), metadata_json={'business_task_context': proof}))
    elif row.metadata_json != {'business_task_context': proof}:
        raise RuntimeError('business_task_input_binding_changed')
    source = {'source_kind': 'business_task_context', 'source_id': str(source_id), 'source_version': digest}
    invocation.messages[0]['context_origin'] = ContextMessageOrigin.capture(kind='business_task', role='user',
        content=proof['message']['content'], sources=[source], unbound=())
    invocation.session_context.metadata['_session_permission_context_source'] = PermissionContextSource(
        source, proof['permission_inputs'], tuple(invocation.allowed_tool_names))
    ensure_runtime_assembly_state(invocation.session_context).record_context_sources(agent_id=run.parent_agent_id,
        content=invocation.system_prompt_suffix, sources=[source], unbound=())
    await db.flush()


async def validate_business_task_context(db, tenant_id, dependency, *, user=None, runtime_request=None):
    from app.core.resource_authority import authorize_resource_action
    from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor

    try:
        row = await db.get(InvocationSpan, uuid.UUID(dependency['source_id']), populate_existing=True)
        proof = (row.metadata_json or {}).get('business_task_context') if row is not None else None
        run = await db.get(RuntimeTask, row.runtime_task_id, populate_existing=True) if row else None
        if (row is None or row.tenant_id != tenant_id or row.span_type != 'business_task_context'
            or row.status != 'captured' or not isinstance(proof, dict) or _hash(proof) != dependency['source_version']
            or run is None or run.tenant_id != tenant_id or row.agent_id != run.parent_agent_id
            or row.user_id != run.root_user_id or row.id != uuid.uuid5(run.id, f"business-task-context:{dependency['source_version']}")
            or not isinstance(proof.get('task'), dict) or row.session_id != proof['task']['session_id']):
            raise ValueError('source_changed')
        executing_source_run = False
        if runtime_request is not None:
            _principal, user = await _runtime_input_actor(db, tenant_id, runtime_request)
            executing_source_run = str(_principal.runtime_task_id) == str(run.id)
        if user is None:
            raise ValueError('reader_unavailable')
        live = await _facts(db, run, require_active=executing_source_run)
        # Reading a saved version preserves its original task text and plan. Using
        # that input to execute or resume its run still requires the current input.
        version_fields = {'title', 'description', 'type', 'request_hash', 'plan_id', 'plan_version', 'plan_hash'}
        if (live is None or set(proof['task']) != set(live)
            or any(proof['task'][key] != value for key, value in live.items()
                if executing_source_run or key not in version_fields)):
            raise ValueError('source_changed')
        task = await db.get(Task, uuid.UUID(proof['task']['task_id']), populate_existing=True)
        await authorize_resource_action(db, user, agent_id=task.agent_id, resource_kind='task', resource_id=task.id,
            action='read', owner_user_id=task.created_by, root_session_id=task.root_session_id,
            authority_state=task.authority_state)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ContentInputReceiptError('business_task_source_changed', 'Review the current business task input.', 403) from exc
