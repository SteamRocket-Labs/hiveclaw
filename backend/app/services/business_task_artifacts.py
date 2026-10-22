"""Saved native task file references, consumed under the original work authority."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.services.content_input_receipts import ContentInputReceiptError


async def _execution_session(db, run):
    try:
        session = await db.get(ChatSession, uuid.UUID(run.child_session_id), populate_existing=True)
        root = uuid.UUID(run.root_session_id or run.parent_session_id or run.child_session_id)
        parent = uuid.UUID(run.parent_session_id) if run.parent_session_id else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError('business_artifact_scope_unavailable', 'Restore the task execution reference.', 503) from exc
    if (run.task_type != 'business_task' or session is None or session.runtime_task_id != run.id
        or session.tenant_id != run.tenant_id or session.agent_id != run.parent_agent_id
        or session.user_id != run.root_user_id or session.session_kind != 'user_task'
        or (session.root_session_id or session.id) != root or session.parent_session_id != parent):
        raise ContentInputReceiptError('business_artifact_scope_changed', 'The task file authority changed.', 403)
    return session, root


async def capture_business_task_artifacts(db, run):
    """Select model-declared files from committed native output receipts once."""
    from app.models.session_v2 import SessionModelResult
    from app.services.chat_artifact_delivery import _native_artifact_for_final_message
    from app.services.direct_invocation_terminal_boundary_processor import _latest_run_event, _validate_response_payload
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch
    from app.services.session_model_inputs import _model_dependencies
    from app.services.web_chat_runtime import _terminal_artifact_candidate_paths

    metadata = dict(run.metadata_json or {})
    outcome = metadata.get('outcome') or {}
    paths = _terminal_artifact_candidate_paths(str(outcome.get('result') or '')) if run.status == 'completed' else []
    selected, errors = [], []
    if paths:
        try:
            session, root = await _execution_session(db, run)
            event = await _latest_run_event(db, task=run, agent_id=run.parent_agent_id,
                session_id=str(session.id), lock_rows=False)
            payload = outcome.get('response_complete_payload')
            if event is None or event.event_type != 'result_commit.round_committed' or not isinstance(payload, dict):
                raise ContentInputReceiptError('business_artifact_result_unavailable', 'Reconcile the saved task result.', 503)
            await _validate_response_payload(db=db, task=run, agent_id=run.parent_agent_id,
                session_id=str(session.id), source='task', terminal_event=event, payload=payload)
            model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run.id,
                SessionModelResult.tenant_id == run.tenant_id, SessionModelResult.session_id == session.id,
                SessionModelResult.round_committed_event_id == event.id, SessionModelResult.state == 'round_committed'))
            if model is None or model.seal_json.get('semantic_content') != outcome.get('result') or not _model_dependencies(model)[1]:
                raise ContentInputReceiptError('business_artifact_sources_unavailable', 'Reconcile the saved file sources.', 503)
            for path in paths:
                _handled, artifact = await _native_artifact_for_final_message(db, tenant_id=run.tenant_id,
                    agent_id=run.parent_agent_id, session_id=session.id, runtime_task_id=run.id, path=path)
                if artifact is None or artifact.owner_user_id != run.root_user_id or artifact.root_session_id != root:
                    errors.append({'path': path, 'reason_code': 'business_artifact_receipt_unavailable'})
                elif str(artifact.id) not in selected:
                    selected.append(str(artifact.id))
        except (ContentInputReceiptError, TerminalBoundaryCanonicalMismatch) as exc:
            errors = [{'path': path, 'reason_code': getattr(exc, 'code', 'business_artifact_result_changed')} for path in paths]
    metadata['business_artifact_ids'] = selected
    metadata['business_artifact_delivery'] = {
        'status': ('partial' if selected else 'unavailable') if errors else ('complete' if selected else 'none'),
        'files': errors,
        **({'recovery_action': 'reconcile_artifact_publication'} if errors else {}),
    }
    run.metadata_json = metadata


async def business_delivery_artifact_session(db, item):
    """Allow the exact execution child without granting another work's files."""
    run = await db.get(RuntimeTask, item.runtime_task_id, populate_existing=True)
    if (run is None or run.tenant_id != item.tenant_id or run.parent_agent_id != item.agent_id
        or run.parent_session_id != str(item.session_id) or run.root_user_id != item.user_id
        or (run.metadata_json or {}).get('business_task_id') != item.metadata.get('business_task_id')
        or run.status != item.terminal_status or run.status != 'completed'
        or (run.metadata_json or {}).get('business_artifact_ids') != [str(value) for value in item.artifact_ids]):
        raise ContentInputReceiptError('business_artifact_binding_changed', 'The saved task file selection changed.', 403)
    return await _execution_session(db, run)


async def read_business_task_artifacts(db, task, user):
    """Return only currently readable saved files; inspection grants no source access."""
    from app.core.resource_authority import authorize_resource_action
    from app.core.source_read_purpose import source_recipient_read
    from app.services.chat_artifact_delivery import artifact_part_from_model

    if user is None or task.active_runtime_task_id is None:
        return [], {}
    run = await db.get(RuntimeTask, task.active_runtime_task_id, populate_existing=True)
    if (run is None or run.tenant_id != task.tenant_id or run.parent_agent_id != task.agent_id
        or (run.metadata_json or {}).get('business_task_id') != str(task.id)):
        return [], {'status': 'unavailable', 'recovery_action': 'reconcile_artifact_publication'}
    metadata = run.metadata_json or {}
    state = metadata.get('business_artifact_delivery') or {}
    public_state = {key: state[key] for key in ('status', 'recovery_action') if key in state}
    ids = metadata.get('business_artifact_ids') or []
    if not ids:
        return [], public_state
    try:
        session, root = await _execution_session(db, run)
        parsed = [uuid.UUID(value) for value in ids]
    except (ContentInputReceiptError, ValueError, TypeError):
        return [], {'status': 'unavailable', 'recovery_action': 'reconcile_artifact_publication'}
    rows = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.id.in_(parsed),
        ChatArtifact.tenant_id == run.tenant_id, ChatArtifact.agent_id == run.parent_agent_id,
        ChatArtifact.session_id == session.id, ChatArtifact.root_session_id == root,
        ChatArtifact.runtime_task_id == run.id, ChatArtifact.owner_user_id == run.root_user_id)))
    by_id, parts = {row.id: row for row in rows}, []
    for artifact_id in parsed:
        artifact = by_id.get(artifact_id)
        if artifact is None:
            public_state = {'status': 'unavailable', 'recovery_action': 'reconcile_artifact_publication'}
            continue
        try:
            with source_recipient_read(session_id=uuid.UUID(run.parent_session_id) if run.parent_session_id else session.id,
                user_id=user.id):
                await authorize_resource_action(db, user, agent_id=run.parent_agent_id, resource_kind='chat_artifact',
                    resource_id=artifact.id, action='read', owner_user_id=artifact.owner_user_id,
                    root_session_id=root, authority_state=artifact.authority_state)
            parts.append(artifact_part_from_model(artifact))
        except (HTTPException, ContentInputReceiptError) as exc:
            public_state = {'status': 'denied' if exc.status_code < 500 else 'unavailable',
                'recovery_action': 'restore_source_access'}
    return parts, public_state


async def business_task_output_visibility(db, task, user, *, include_previous_attempts=False):
    """Task governance access does not disclose source-derived results or logs."""
    from app.core.source_read_purpose import source_recipient_read
    from app.models.session_v2 import SessionModelResult
    from app.services.learned_skill_sources import model_source, validate_model_source

    query = select(RuntimeTask).where(RuntimeTask.tenant_id == task.tenant_id,
        RuntimeTask.parent_agent_id == task.agent_id, RuntimeTask.task_type == 'business_task',
        RuntimeTask.metadata_json['business_task_id'].astext == str(task.id))
    if not include_previous_attempts:
        query = query.where(RuntimeTask.id == task.active_runtime_task_id)
    runs = list(await db.scalars(query))
    models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.tenant_id == task.tenant_id,
        SessionModelResult.run_id.in_([run.id for run in runs])))) if runs else []
    try:
        # Logs include prior attempts; their sources cannot block a new result.
        if any((run.metadata_json or {}).get('outcome', {}).get('result')
            and not any(model.run_id == run.id for model in models) for run in runs):
            raise ContentInputReceiptError('business_result_sources_unavailable', 'Reconcile the saved task result.', 503)
        if models and user is None:
            raise ContentInputReceiptError('business_result_reader_unavailable', 'Restore the result reader authority.', 403)
        for model in models:
            with source_recipient_read(session_id=task.root_session_id or model.session_id, user_id=user.id):
                await validate_model_source(db, task.tenant_id, model_source(model), user=user)
        return {'status': 'available'}
    except (HTTPException, ContentInputReceiptError) as exc:
        return {'status': 'denied' if exc.status_code < 500 else 'unavailable',
            'recovery_action': 'restore_source_access' if exc.status_code < 500 else 'reconcile_task_sources'}
