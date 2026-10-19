"""A2A transport bindings over the existing durable Session and RuntimeTask."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.services.machine_runtime_authority import machine_runtime_actor

MACHINE_TASK_METHOD_SCOPES = {'SendMessage': 'agent.execute', 'GetTask': 'task.read', 'CancelTask': 'task.cancel'}


class A2AError(ValueError):
    def __init__(self, code, message, *, data=None):
        self.code, self.message, self.data = code, message, data
        super().__init__(message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise A2AError(-32001, 'Task not found') from None


async def _machine_runtime_task(db, context, task_id):
    task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == _uuid(task_id),
        RuntimeTask.tenant_id == context.tenant_id, RuntimeTask.parent_agent_id == context.agent_id,
        RuntimeTask.root_external_principal_id == context.principal_id,
        RuntimeTask.root_user_id.is_(None), RuntimeTask.machine_credential_id.is_not(None),
        RuntimeTask.root_runtime_task_id == RuntimeTask.id,
        RuntimeTask.task_type != 'approval_execution').execution_options(populate_existing=True))
    if task is None:
        return None
    successor = await db.scalar(select(RuntimeTask).where(RuntimeTask.root_runtime_task_id == task.id,
        RuntimeTask.id != task.id, RuntimeTask.task_type != 'approval_execution',
        RuntimeTask.tenant_id == context.tenant_id, RuntimeTask.parent_agent_id == context.agent_id,
        RuntimeTask.parent_session_id == task.parent_session_id, RuntimeTask.root_user_id.is_(None),
        RuntimeTask.root_external_principal_id == context.principal_id,
        RuntimeTask.machine_credential_id == task.machine_credential_id)
        .order_by(RuntimeTask.created_at.desc(), RuntimeTask.id.desc()).limit(1).execution_options(populate_existing=True))
    return successor or task


async def _machine_question(db, task):
    from app.services.session_user_question import question_payload, question_requester_matches

    rows = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id == task.tenant_id,
        ChatTranscriptEvent.agent_id == task.parent_agent_id, ChatTranscriptEvent.session_id == _uuid(task.parent_session_id),
        ChatTranscriptEvent.run_id == task.id, ChatTranscriptEvent.item_kind == 'user_question')
        .order_by(ChatTranscriptEvent.sequence.desc())))
    seen = set()
    for row in rows:
        if row.item_id in seen:
            continue
        seen.add(row.item_id)
        if row.lifecycle in {'created', 'waiting'} and question_requester_matches(task, question_payload(row)):
            return row
    return None


def _machine_task_inputs(context, public_task_id, task):
    from app.models.session_v2 import SessionCommand, SessionTurnInput

    return select(SessionTurnInput).join(SessionCommand, SessionCommand.id == SessionTurnInput.command_id).where(
        SessionTurnInput.tenant_id == context.tenant_id, SessionCommand.tenant_id == context.tenant_id,
        SessionTurnInput.session_id == _uuid(task.parent_session_id), SessionCommand.session_id == SessionTurnInput.session_id,
        SessionCommand.principal_type == 'external_principal', SessionCommand.principal_id == context.principal_id,
        SessionCommand.target_json['runtime_metadata']['root_runtime_task_id'].astext == str(public_task_id),
        SessionCommand.target_json['session_command_authority']['schema'].astext == 'hive.session_command_authority.machine.v1',
        SessionCommand.target_json['session_command_authority']['credential_id'].astext == str(task.machine_credential_id))


async def _pending_machine_input(db, context, public_task_id, task):
    from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput

    return await db.scalar(_machine_task_inputs(context, public_task_id, task).join(SessionInputAdmission,
        (SessionInputAdmission.input_id == SessionTurnInput.id)
        & (SessionInputAdmission.command_id == SessionCommand.id)
        & (SessionInputAdmission.input_revision == SessionTurnInput.revision)).where(
            SessionInputAdmission.tenant_id == context.tenant_id,
            SessionTurnInput.status.in_({'accepted', 'queued', 'rolled_over'}),
            SessionInputAdmission.state == 'admitted', SessionInputAdmission.dispatch_state.in_({'pending', 'dispatching'}))
        .order_by(SessionTurnInput.queue_ordinal).limit(1))


async def _cancelled_machine_input(db, context, public_task_id, task):
    from app.models.session_v2 import SessionCommand, SessionTurnInput

    return await db.scalar(_machine_task_inputs(context, public_task_id, task).where(
        SessionTurnInput.status == 'cancelled', SessionTurnInput.bound_round_id.is_(None),
        SessionCommand.status == 'rejected', SessionCommand.rejection_json['reason_code'].astext == 'cancelled_before_bind')
        .order_by(SessionTurnInput.queue_ordinal.desc()).limit(1))


def _machine_input_message(context, public_task_id, session_id, row, command):
    original = ((command.target_json or {}).get('runtime_metadata') or {}).get('a2a_input_message') or {}
    message_id = original.get('messageId')
    if (not isinstance(message_id, str) or not message_id
        or uuid.uuid5(context.principal_id, 'a2a-message:' + message_id) != row.id):
        raise A2AError(-32603, 'Committed task input identity is unavailable')
    # The canonical input has passed the trusted ingress secret boundary.
    # Raw transport metadata must never undo its exact-byte redactions.
    parts = [{'text': part['text']} for part in row.content_parts_json or []
        if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str)]
    if not parts:
        raise A2AError(-32603, 'Committed task input is unavailable')
    return {'messageId': message_id, 'contextId': str(session_id), 'taskId': str(public_task_id),
        'role': 'ROLE_USER', 'parts': parts}


def _machine_question_message(question, public_task_id, session_id):
    details = (question.metadata_json or {}).get('v2_payload') or {}
    return {'messageId': str(question.id), 'contextId': str(session_id), 'taskId': str(public_task_id),
        'role': 'ROLE_AGENT', 'parts': [{'data': {'requestItemId': str(question.item_id),
            'questions': details.get('questions', [])}, 'mediaType': 'application/json'}]}


async def _machine_task_history(db, context, public_task_id, task, *, semantic_history=None):
    """Project one logical task from its original commands and canonical journal."""
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.session_user_question import question_payload, question_requester_matches

    session_id = _uuid(task.parent_session_id)
    runs = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == context.tenant_id,
        RuntimeTask.parent_agent_id == context.agent_id, RuntimeTask.parent_session_id == str(session_id),
        RuntimeTask.root_runtime_task_id == public_task_id, RuntimeTask.root_user_id.is_(None),
        RuntimeTask.root_external_principal_id == context.principal_id,
        RuntimeTask.machine_credential_id == task.machine_credential_id, RuntimeTask.task_type != 'approval_execution')))
    by_run = {run.id: run for run in runs}
    events = list(await db.scalars(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == context.tenant_id, ChatTranscriptEvent.agent_id == context.agent_id,
        ChatTranscriptEvent.session_id == session_id, ChatTranscriptEvent.run_id.in_(by_run))
        .order_by(ChatTranscriptEvent.sequence)))
    allowed_events = {str(event.id) for event in events}
    candidates = list((await db.execute(select(SessionTurnInput, SessionCommand).join(SessionCommand,
        SessionCommand.id == SessionTurnInput.command_id).where(SessionTurnInput.tenant_id == context.tenant_id,
        SessionCommand.tenant_id == context.tenant_id, SessionTurnInput.session_id == session_id,
        SessionCommand.session_id == session_id, SessionCommand.principal_type == 'external_principal',
        SessionCommand.principal_id == context.principal_id,
        SessionCommand.target_json['session_command_authority']['schema'].astext == 'hive.session_command_authority.machine.v1',
        SessionCommand.target_json['session_command_authority']['credential_id'].astext == str(task.machine_credential_id),
        SessionCommand.target_json['runtime_metadata']['source'].astext == 'a2a'))).all())
    run_ids = {str(run_id) for run_id in by_run}
    inputs = [(row, command) for row, command in candidates
        if uuid.uuid5(row.id, 'session-v2-runtime-run') in by_run
        or ((command.target_json or {}).get('runtime_metadata') or {}).get('root_runtime_task_id') == str(public_task_id)
        or (row.intent in {'answer_request', 'steer_current_turn'}
            and (str(row.target_run_id) in run_ids or (command.target_json or {}).get('expected_run_id') in run_ids))]
    if not any(uuid.uuid5(row.id, 'session-v2-runtime-run') == public_task_id for row, _command in inputs):
        raise A2AError(-32603, 'Committed task input identity is unavailable')
    sequences = dict((await db.execute(select(ChatTranscriptEvent.input_id, func.min(ChatTranscriptEvent.sequence))
        .where(ChatTranscriptEvent.tenant_id == context.tenant_id, ChatTranscriptEvent.agent_id == context.agent_id,
            ChatTranscriptEvent.session_id == session_id, ChatTranscriptEvent.input_id.in_([row.id for row, _command in inputs]),
            ChatTranscriptEvent.item_kind == 'human_input', ChatTranscriptEvent.lifecycle == 'accepted')
        .group_by(ChatTranscriptEvent.input_id))).all())
    ordered = []
    for row, command in inputs:
        if row.id not in sequences:
            raise A2AError(-32603, 'Committed task input order is unavailable')
        ordered.append((sequences[row.id], _machine_input_message(context, public_task_id, session_id, row, command)))
    questions = set()
    for event in events:
        if (event.item_kind == 'user_question' and event.lifecycle in {'created', 'waiting'}
            and event.item_id not in questions and question_requester_matches(by_run[event.run_id], question_payload(event))):
            questions.add(event.item_id)
            ordered.append((event.sequence, _machine_question_message(event, public_task_id, session_id)))
    if semantic_history is None:
        semantic_history = await load_session_semantic_history(db, tenant_id=context.tenant_id,
            agent_id=context.agent_id, session_id=session_id)
    for message in semantic_history.messages:
        if message.role == 'assistant' and message.content and allowed_events.intersection(message.source_event_ids):
            ordered.append((message.sequence, {'messageId': message.id, 'contextId': str(session_id),
                'taskId': str(public_task_id), 'role': 'ROLE_AGENT', 'parts': [{'text': message.content}]}))
    return [message for _sequence, message in sorted(ordered, key=lambda item: (item[0], item[1]['messageId']))]


async def _machine_task_artifacts(db, context, task):
    """Read the committed selection, never enumerate the agent's workspace."""
    from app.models.chat_artifact import ChatArtifact
    from app.models.session_v2 import SessionRunOutcome
    from app.services.chat_artifact_delivery import artifact_part_from_model
    from app.services.session_terminal_outcome import _sha256

    outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.tenant_id == context.tenant_id,
        SessionRunOutcome.session_id == _uuid(task.parent_session_id), SessionRunOutcome.run_id == task.id,
        SessionRunOutcome.state == 'terminal_committed'))
    if outcome is None:
        raise A2AError(-32603, 'Committed task result is unavailable')
    seal = outcome.seal_json or {}
    parts = seal.get('parts') or []
    if not isinstance(parts, list) or _sha256(parts) != seal.get('artifact_manifest_hash'):
        raise A2AError(-32603, 'Committed task artifacts are unavailable')
    artifacts = []
    for part in parts:
        if not isinstance(part, dict) or part.get('type') != 'artifact':
            raise A2AError(-32603, 'Committed task artifacts are unavailable')
        row = await db.get(ChatArtifact, _uuid(part.get('artifact_id')), populate_existing=True)
        if (row is None or row.tenant_id != context.tenant_id or row.agent_id != context.agent_id
            or row.session_id != _uuid(task.parent_session_id) or row.runtime_task_id != task.id
            or row.owner_user_id is not None or row.authority_state != 'owned'
            or str(row.root_session_id) != str(task.root_session_id or task.parent_session_id)
            or artifact_part_from_model(row) != part):
            raise A2AError(-32603, 'Committed task artifacts are unavailable')
        artifacts.append(row)
    return artifacts


async def get_machine_artifact(db, context, task_id, artifact_id):
    """Resolve a file only through this caller's committed task selection."""
    task = await _machine_runtime_task(db, context, task_id)
    if task is None or task.status != 'completed':
        raise A2AError(-32001, 'Task artifact not found')
    rows = await _machine_task_artifacts(db, context, task)
    artifact = next((row for row in rows if row.id == artifact_id), None)
    if artifact is None:
        raise A2AError(-32001, 'Task artifact not found')
    from app.services.content_input_receipts import validate_artifact_content_receipt
    from app.services.machine_runtime_authority import MachineArtifactReader

    reader = MachineArtifactReader(agent_id=context.agent_id, requester_user_id=None,
        session_id=task.parent_session_id, runtime_task_id=str(task.id), purpose='machine_task')
    await validate_artifact_content_receipt(db, context.tenant_id, None, artifact, runtime_principal=reader)
    return artifact


async def get_machine_task(db, context, task_id, *, history_length=None):
    public_task_id = _uuid(task_id)
    task = await _machine_runtime_task(db, context, task_id)
    if task is None:
        from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput

        ingress = await db.get(ChannelIngressEvent, uuid.uuid5(_uuid(task_id), 'ingress'), populate_existing=True)
        if (ingress is None or ingress.tenant_id != context.tenant_id or ingress.agent_id != context.agent_id
            or ingress.provider != 'a2a' or ingress.external_principal_id != context.principal_id):
            raise A2AError(-32001, 'Task not found')
        input_id = _uuid((ingress.metadata_json or {}).get('input_id'))
        row = await db.get(SessionTurnInput, input_id, populate_existing=True)
        command = await db.get(SessionCommand, row.command_id, populate_existing=True) if row is not None else None
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id)
            .order_by(SessionInputAdmission.input_revision.desc()).limit(1)) if row is not None else None
        if (row is None or command is None or admission is None or command.principal_type != 'external_principal'
            or command.principal_id != context.principal_id or command.session_id != ingress.result_session_id
            or uuid.uuid5(input_id, 'session-v2-runtime-run') != _uuid(task_id)):
            raise A2AError(-32001, 'Task not found')
        state = ('TASK_STATE_CANCELED' if row.status == 'cancelled'
            else 'TASK_STATE_REJECTED' if admission.state in {'rejected', 'cancelled'}
            else 'TASK_STATE_UNSPECIFIED' if admission.state == 'needs_reconciliation' else 'TASK_STATE_SUBMITTED')
        payload = {'id': str(task_id), 'contextId': str(ingress.result_session_id), 'status': {'state': state},
            'metadata': {'hive': {'admissionStatus': admission.state, 'dispatchStatus': admission.dispatch_state}}}
        if history_length != 0:
            payload['history'] = [_machine_input_message(context, public_task_id, ingress.result_session_id, row, command)]
        return payload
    states = {'pending': 'TASK_STATE_SUBMITTED', 'running': 'TASK_STATE_WORKING',
        'completed': 'TASK_STATE_COMPLETED', 'failed': 'TASK_STATE_FAILED', 'killed': 'TASK_STATE_CANCELED',
        'suspended': 'TASK_STATE_INPUT_REQUIRED', 'resumable': 'TASK_STATE_SUBMITTED'}
    events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id == context.tenant_id,
        ChatTranscriptEvent.agent_id == context.agent_id, ChatTranscriptEvent.run_id == task.id,
        ChatTranscriptEvent.event_type.in_(['assistant_message', 'user_message']))
        .order_by(ChatTranscriptEvent.sequence)))
    semantic_history = None
    payload = {'id': str(public_task_id), 'contextId': task.parent_session_id,
        'status': {'state': states.get(task.status, 'TASK_STATE_UNSPECIFIED')},
        'metadata': {'hive': {'runtimeStatus': task.status}}}
    pause = (task.metadata_json or {}).get('interactive_pause')
    if pause == 'awaiting_user_clarification' and task.status == 'completed':
        payload['status']['state'] = 'TASK_STATE_INPUT_REQUIRED'
    elif pause == 'awaiting_session_permission' and task.status == 'suspended':
        payload['status']['state'] = 'TASK_STATE_AUTH_REQUIRED'
    elif pause == 'awaiting_company_approval' and task.status in {'completed', 'suspended'}:
        payload['status']['state'] = 'TASK_STATE_AUTH_REQUIRED'
    if await _pending_machine_input(db, context, public_task_id, task) is not None:
        payload['status']['state'] = 'TASK_STATE_SUBMITTED'
    elif task.status == 'completed' and await _cancelled_machine_input(db, context, public_task_id, task) is not None:
        payload['status']['state'] = 'TASK_STATE_CANCELED'
        payload['metadata']['hive']['cancellationStatus'] = 'applied'
    if payload['status']['state'] == 'TASK_STATE_INPUT_REQUIRED':
        question = await _machine_question(db, task)
        if question is not None:
            payload['status']['message'] = _machine_question_message(question, public_task_id, task.parent_session_id)
    # Full durable assistant bytes, never RuntimeTask.result_summary's bounded projection.
    outputs = [row for row in events if row.event_type == 'assistant_message']
    if payload['status']['state'] == 'TASK_STATE_COMPLETED' and outputs:
        payload['artifacts'] = [{'artifactId': str(row.id), 'name': 'Response', 'parts': [{'text': row.content}]}
            for row in outputs]
    elif payload['status']['state'] == 'TASK_STATE_COMPLETED':
        from app.models.session_v2 import SessionRunOutcome
        from app.services.session_semantic_history import load_session_semantic_history

        outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.run_id == task.id,
            SessionRunOutcome.tenant_id == context.tenant_id, SessionRunOutcome.session_id == _uuid(task.parent_session_id),
            SessionRunOutcome.state == 'terminal_committed'))
        if outcome is None:
            raise A2AError(-32603, 'Committed task result is unavailable')
        semantic_history = await load_session_semantic_history(db, tenant_id=context.tenant_id, agent_id=context.agent_id,
            session_id=_uuid(task.parent_session_id))
        final = next((message for message in semantic_history.messages
            if message.id == f'session-model-result:{outcome.terminal_result_id}' and message.role == 'assistant'), None)
        if final is None:
            raise A2AError(-32603, 'Committed task result is unavailable')
        payload['artifacts'] = [{'artifactId': str(outcome.id), 'name': 'Response', 'parts': [{'text': final.content or ''}]}]
    if payload['status']['state'] == 'TASK_STATE_COMPLETED':
        artifacts = await _machine_task_artifacts(db, context, task)
        payload.setdefault('artifacts', []).extend({'artifactId': str(row.id), 'name': row.name,
            'parts': [{'url': f'/agents/{context.agent_id}/a2a/tasks/{public_task_id}/artifacts/{row.id}',
                'filename': row.name, 'mediaType': row.mime_type or 'application/octet-stream'}]} for row in artifacts)
    if history_length != 0:
        messages = await _machine_task_history(db, context, public_task_id, task, semantic_history=semantic_history)
        payload['history'] = messages if history_length is None else messages[-history_length:]
    return payload


class _TaskTransitionWake:
    """Observe status signals only; the authenticated database read owns every byte."""
    def __init__(self):
        self.changed, self.closed = asyncio.Event(), False

    async def accept(self):
        pass

    async def send_json(self, event):
        # A Session can create a successor while a query is in flight. All
        # terminal signals wake the exact authenticated task read; no event
        # content or caller-provided run ID becomes task authority.
        # An input cancelled before dispatch has no run ID at all.
        if (event.get('type') in {'done', 'error'}
            or (event.get('type') == 'phase' and event.get('phase') in {'done', 'failed', 'cancelled', 'awaiting_approval', 'awaiting_budget'})
            or (event.get('item_kind') == 'run' and event.get('lifecycle') in {'completed', 'failed', 'cancelled', 'suspended'})
            or (event.get('item_kind') in {'human_input', 'input_admission'}
                and event.get('lifecycle') in {'cancelled', 'rejected', 'needs_reconciliation'})):
            self.changed.set()

    async def close(self, **_kwargs):
        self.closed = True
        self.changed.set()


async def wait_for_machine_task(db, context, task, *, history_length=None):
    from app.services.machine_identity_service import load_machine_grant
    from app.services.web_chat_runtime import web_chat_broker

    wake = _TaskTransitionWake()
    await db.commit()  # Do not hold credential or Session locks while awaiting a worker.
    await web_chat_broker.connect(str(context.agent_id), wake, task['contextId'])
    try:
        while True:
            wake.changed.clear()
            await load_machine_grant(db, credential_id=context.credential_id, agent_id=context.agent_id,
                principal_id=context.principal_id, scope='agent.execute')
            current = await get_machine_task(db, context, task['id'], history_length=history_length)
            await db.commit()
            state = current['status']['state']
            if state not in {'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING'}:
                return current
            if wake.closed:
                raise A2AError(-32603, 'Task status delivery is unavailable', data=[{
                    '@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'reason': 'TASK_STATUS_UNAVAILABLE',
                    'domain': 'hiveclaw', 'metadata': {'taskId': current['id'], 'nextAction': 'GetTask'}}])
            await wake.changed.wait()
    finally:
        await web_chat_broker.disconnect(str(context.agent_id), wake)


async def cancel_machine_task(db, context, agent, task_id):
    from app.models.session_v2 import SessionTurnInput
    from app.services.chat_transcript import lock_transcript_session
    from app.services.session_human_input import cancel_unbound_human_input
    from app.services.session_live_input import submit_live_cancel_input
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    current = await get_machine_task(db, context, task_id, history_length=0)
    session_id = _uuid(current['contextId'])
    actor = await machine_runtime_actor(db, context)
    authority = await resolve_session_mutation_authority(db, user=actor, agent_id=context.agent_id,
        session_id=session_id, action='mutate_session_control')
    await lock_transcript_session(db, session_id=session_id)
    task = await _machine_runtime_task(db, context, task_id)
    current = await get_machine_task(db, context, task_id, history_length=0)
    if current['status']['state'] == 'TASK_STATE_CANCELED':
        return current
    if task is None:
        ingress = await db.get(ChannelIngressEvent, uuid.uuid5(_uuid(task_id), 'ingress'))
        row = await db.get(SessionTurnInput, _uuid(ingress.metadata_json['input_id']))
        await cancel_unbound_human_input(db, authority=authority, input_id=row.id, expected_revision=row.revision)
        await db.commit()
        return await get_machine_task(db, context, task_id, history_length=0)
    if task.status == 'killed':
        return current
    cancelled_input = False
    while (pending := await _pending_machine_input(db, context, _uuid(task_id), task)) is not None:
        await cancel_unbound_human_input(db, authority=authority, input_id=pending.id,
            expected_revision=pending.revision, reopen_answer_request=False)
        await db.flush()
        cancelled_input = True
    if cancelled_input and task.status == 'completed':
        await db.commit()
        return await get_machine_task(db, context, task_id, history_length=0)
    if task.status not in {'pending', 'running', 'suspended', 'resumable'}:
        raise A2AError(-32002, 'Task is not cancelable')
    session = await db.get(ChatSession, session_id, populate_existing=True)
    receipt = await submit_live_cancel_input(db=db, agent=agent, user=actor, session=session,
        run_id=task.id, source='a2a')
    result = await get_machine_task(db, context, task_id, history_length=0)
    result['metadata']['hive']['cancellationStatus'] = receipt['status']
    return result


async def _protect_machine_ingress(db, context, immutable, bearer_secret):
    from app.services.credential_boundary_loader import (
        RuntimeIngressSecretBoundaryUnavailable, exact_secret_redaction_receipt, load_runtime_ingress_secret_boundary,
    )
    from app.services.exact_secret_boundary import ExactSecretBoundary

    try:
        inventory = await load_runtime_ingress_secret_boundary(db, tenant_id=context.tenant_id, agent_id=context.agent_id)
    except RuntimeIngressSecretBoundaryUnavailable as exc:
        raise A2AError(-32603, 'Task ingress credential boundary is unavailable') from exc
    boundary = ExactSecretBoundary.combine(inventory, ExactSecretBoundary.from_pairs([
        (f'machine-credential://{context.credential_id}/token', bearer_secret)]))
    redaction = boundary.redact_payload_with_evidence(immutable)
    protected = redaction.value
    message = protected.get('message') or {}
    if (not isinstance(message, dict) or any(message.get(key) != immutable['message'].get(key)
        for key in ('messageId', 'taskId', 'contextId', 'role'))
        or not isinstance(message.get('parts'), list)
        or any(not isinstance(part, dict) or not isinstance(part.get('text'), str) for part in message['parts'])):
        raise A2AError(-32602, 'Invalid message identity or target')
    return protected, exact_secret_redaction_receipt(redaction, phase='a2a_machine_message')


async def send_machine_message(db, context, agent, params, *, bearer_secret):
    from app.services.session_live_input import submit_live_human_input

    message = params.get('message')
    if (not isinstance(message, dict) or message.get('role') != 'ROLE_USER'
        or not isinstance(message.get('messageId'), str) or not message['messageId']
        or len(message['messageId']) > 512 or not isinstance(message.get('parts'), list) or not message['parts']):
        raise A2AError(-32602, 'Invalid parameters')
    parts = message['parts']
    if any(not isinstance(part, dict) or not isinstance(part.get('text'), str)
        or set(part) - {'text', 'metadata'} for part in parts):
        raise A2AError(-32005, 'Unsupported content type')
    content = '\n'.join(part['text'] for part in parts)
    if not content:
        raise A2AError(-32602, 'Invalid parameters')
    configuration = params.get('configuration') or {}
    if not isinstance(configuration, dict):
        raise A2AError(-32602, 'Invalid parameters')
    if configuration.get('taskPushNotificationConfig'):
        raise A2AError(-32003, 'Push notifications are not supported')
    if 'returnImmediately' in configuration and type(configuration['returnImmediately']) is not bool:
        raise A2AError(-32602, 'Invalid returnImmediately value')
    immutable = {'message': message, 'metadata': params.get('metadata')}
    try:
        encoded = json.dumps(immutable, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise A2AError(-32602, 'Invalid parameters') from None
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    input_id = uuid.uuid5(context.principal_id, 'a2a-message:' + message['messageId'])
    run_id = uuid.uuid5(input_id, 'session-v2-runtime-run')
    public_task_id = _uuid(message['taskId']) if message.get('taskId') else run_id
    previous = await db.get(RuntimeTask, run_id, populate_existing=True)
    if previous is not None:
        if (previous.root_external_principal_id != context.principal_id or previous.parent_agent_id != agent.id
            or previous.tenant_id != context.tenant_id or (previous.metadata_json or {}).get('a2a_message_hash') != digest):
            raise A2AError(-32602, 'Message identity conflicts with an existing request')
        return {'task': await get_machine_task(db, context, public_task_id, history_length=configuration.get('historyLength'))}
    retained = await db.get(ChannelIngressEvent, uuid.uuid5(run_id, 'ingress'), populate_existing=True)
    if retained is not None:
        if retained.payload_digest != digest or retained.external_principal_id != context.principal_id:
            raise A2AError(-32602, 'Message identity conflicts with an existing request')
        return {'task': await get_machine_task(db, context, public_task_id, history_length=configuration.get('historyLength'))}
    # The shared admission can commit before the transport receipt. Recover
    # that exact nonce from the canonical command before considering new work.
    from app.models.session_v2 import SessionCommand, SessionTurnInput

    admitted = await db.get(SessionTurnInput, input_id, populate_existing=True)
    replayed = False
    question = None
    continuation_metadata = {}
    if admitted is not None:
        command = await db.get(SessionCommand, admitted.command_id, populate_existing=True)
        metadata = (command.target_json or {}).get('runtime_metadata') or {} if command else {}
        if (command is None or command.tenant_id != context.tenant_id or command.session_id != admitted.session_id
            or command.principal_type != 'external_principal' or command.principal_id != context.principal_id
            or metadata.get('a2a_message_hash') != digest):
            raise A2AError(-32602, 'Message identity conflicts with an existing request')
        session = await db.get(ChatSession, admitted.session_id, populate_existing=True)
        immutable = {'message': metadata.get('a2a_input_message') or {}, 'metadata': metadata.get('a2a_request_metadata')}
        replayed = True
    immutable, ingress_redaction = await _protect_machine_ingress(db, context, immutable, bearer_secret)
    message = immutable['message']
    content = '\n'.join(part['text'] for part in message['parts'])
    if replayed:
        pass
    elif message.get('taskId'):
        current = await get_machine_task(db, context, public_task_id, history_length=0)
        from app.services.chat_transcript import lock_transcript_session

        await lock_transcript_session(db, session_id=_uuid(current['contextId']))
        current = await get_machine_task(db, context, public_task_id, history_length=0)
        if message.get('contextId') and _uuid(message['contextId']) != _uuid(current['contextId']):
            raise A2AError(-32602, 'Task and context identifiers do not match')
        if current['status']['state'] in {'TASK_STATE_COMPLETED', 'TASK_STATE_FAILED', 'TASK_STATE_CANCELED', 'TASK_STATE_REJECTED'}:
            raise A2AError(-32004, 'A terminal task cannot accept another message')
        task = await _machine_runtime_task(db, context, public_task_id)
        if task is None:
            raise A2AError(-32004, 'Task admission is not ready for another message')
        session = await db.get(ChatSession, _uuid(task.parent_session_id), populate_existing=True)
        if current['status']['state'] == 'TASK_STATE_INPUT_REQUIRED':
            question = await _machine_question(db, task)
            if question is None:
                raise A2AError(-32603, 'The pending task question is unavailable')
        continuation_metadata = {'root_runtime_task_id': str(public_task_id),
            **({'budget_run_id': str(task.budget_run_id)} if task.budget_run_id else {})}
    elif message.get('contextId'):
        session = await db.get(ChatSession, _uuid(message['contextId']), populate_existing=True)
        if (session is None or session.agent_id != agent.id or session.tenant_id != context.tenant_id
            or session.external_principal_id != context.principal_id or session.user_id is not None):
            raise A2AError(-32001, 'Task context not found')
    else:
        session_id = uuid.uuid5(context.principal_id, 'a2a-context:' + message['messageId'])
        session = await db.get(ChatSession, session_id, populate_existing=True)
        if session is None:
            session = ChatSession(id=session_id, agent_id=agent.id, tenant_id=context.tenant_id, user_id=None,
                external_principal_id=context.principal_id, source_channel='a2a', session_kind='machine_task',
                actor_type='external_principal', runtime_source='a2a', visibility_scope='external_principal', title='Machine task')
            db.add(session)
            await db.flush()
    if (session is None or session.agent_id != agent.id or session.tenant_id != context.tenant_id
        or session.external_principal_id != context.principal_id or session.user_id is not None):
        raise A2AError(-32001, 'Task context not found')
    actor = await machine_runtime_actor(db, context)
    session_id = session.id
    if not replayed:
        await submit_live_human_input(db=db, agent=agent, user=actor, session=session, content=content,
            source='a2a', input_id=input_id, idempotency_key=f'a2a-message:{message["messageId"]}',
            **({'requested_kind': 'answer_request', 'request_item_id': question.item_id,
                'expected_run_id': question.run_id} if question is not None else {}),
            runtime_metadata={'source': 'a2a', 'channel': 'a2a', 'a2a_message_hash': digest,
                'a2a_input_message': message, 'a2a_request_metadata': immutable.get('metadata'),
                **({'exact_secret_ingress_redaction': ingress_redaction} if ingress_redaction is not None else {}),
                **continuation_metadata})
    task = await db.get(RuntimeTask, run_id, populate_existing=True)
    values = dict(id=uuid.uuid5(run_id, 'ingress'), tenant_id=context.tenant_id, agent_id=context.agent_id,
        provider='a2a', installation_ref=f'machine:{context.principal_id}', provider_event_id=message['messageId'],
        handler_key='a2a.machine_message.v1', payload_digest=digest, payload_json=immutable,
        status='processed', external_principal_id=context.principal_id,
        result_runtime_task_id=run_id if task is not None else None, result_session_id=session_id,
        metadata_json={'input_id': str(input_id), 'runtime_task_id': str(run_id)},
        processing_receipt_json={'status': 'admitted', 'runtime_task_id': str(run_id)},
        processed_at=datetime.now(timezone.utc))
    await db.execute(insert(ChannelIngressEvent).values(**values).on_conflict_do_nothing(index_elements=['id']))
    await db.commit()
    return {'task': await get_machine_task(db, context, public_task_id, history_length=configuration.get('historyLength'))}
