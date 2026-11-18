"""Carry approved delegation authority and recheck sources for outward tools."""

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.core.execution_context import A2AToolAuthorityFrame, ExecutionIdentity, ExecutionPrincipal
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.runtime.ccplus_contracts import build_permission_profile, permission_profile_snapshot, permission_profile_snapshot_hash
from app.services.content_input_receipts import (
    ContentInputReceiptError, _preload_execution_sources, _validate_input_dependencies, group_source_read,
    latest_workspace_receipt,
)
from app.services.session_model_inputs import _hash, _model_dependencies, model_output_dependencies


async def delegated_permission_execution(db, *, authority, session, invocation, profile, control_id, model):
    from app.agents.delegation_token import issue_delegation_token
    from app.core.execution_context import A2A_TOOL_AUTHORITY_FRAME_SCHEMA
    from app.services.capability_gate import _resolve_capability
    from app.services.delegation_context_sources import _facts
    from app.services.shared_group_authority import _permission_execution_matches

    task = await db.get(RuntimeTask, invocation.run_id, populate_existing=True)
    if task is None or task.task_type != 'delegation':
        return {}
    if (task.tenant_id != authority.tenant_id or task.child_agent_id != authority.agent_id
        or uuid.UUID(task.child_session_id) != session.id or session.runtime_task_id != task.id
        or session.user_id != authority.principal_id or task.root_user_id != authority.principal_id
        or not await _permission_execution_matches(db, task=task, session=session)
        or await _facts(db, task) is None):
        raise ContentInputReceiptError('delegation_permission_authority_unavailable', 'Restore the original permission authority.', 403)
    metadata = task.metadata_json or {}
    original = metadata['authority_snapshot']
    original_profile = original['permission_profile']
    if metadata.get('authority_policy_hash') != permission_profile_snapshot_hash(original_profile):
        raise ContentInputReceiptError('delegation_permission_authority_unavailable', 'Restore the original delegation policy.', 403)
    # Only the authenticated response adds the exact tool/input grant. All
    # original sandbox, network, tool, path and denial limits remain in force.
    approved = permission_profile_snapshot(profile)
    profile = build_permission_profile({**original_profile, **{key: approved[key] for key in (
        'session_grant_scope', 'session_grant_tool_name', 'session_grant_input_hash', 'session_grants')}})
    snapshot = {**original, 'permission_profile': permission_profile_snapshot(profile),
        'permission_response': {'control_id': str(control_id), 'invocation_id': str(invocation.id),
            'authority_snapshot_hash': invocation.permission_authority_snapshot_hash}}
    capability = _resolve_capability(invocation.tool_name)
    if capability is None:
        raise ContentInputReceiptError('delegation_permission_capability_unavailable', 'Restore the tool capability mapping.', 503)
    token = issue_delegation_token(parent_agent_id=task.parent_agent_id, child_agent_id=task.child_agent_id,
        granted_capabilities={capability})
    identity = original.get('execution_identity') or {}
    identity = ExecutionIdentity(identity_type=identity['identity_type'],
        identity_id=uuid.UUID(identity['identity_id']) if identity.get('identity_id') else None, label=identity.get('label') or '')
    frame = A2AToolAuthorityFrame(schema=A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
        principal=ExecutionPrincipal.from_evidence(original['execution_principal']),
        capability_snapshot_hash=_hash(snapshot), policy_snapshot_hash=permission_profile_snapshot_hash(profile),
        capability_snapshot=snapshot, permission_profile=profile, execution_identity=identity, delegation_token=token,
        session_id=original['session_id'], parent_session_id=original['parent_session_id'], runtime_task_id=original['runtime_task_id'],
        root_runtime_task_id=original.get('root_runtime_task_id'), budget_run_id=original.get('budget_run_id') or None,
        trace_id=original.get('trace_id'), delegation_id=token.delegation_id,
        sandbox_profile=snapshot['permission_profile']['sandbox'], approval_policy=snapshot['permission_profile']['approval_policy'])
    _dependencies, complete = model_output_dependencies(model)
    if not complete:
        raise ContentInputReceiptError('delegation_permission_sources_unavailable', 'Restore the committed tool input inventory.', 503)
    return {'authority_frame': frame, 'execution_identity': identity, 'delegation_token': token,
        'permission_profile': profile, 'budget_run_id': frame.budget_run_id,
        'runtime_task_id': frame.runtime_task_id, 'session_id': frame.session_id,
        't0_refs': (f'session-model-result:{model.id}',)}


async def load_outward_tool_round(db, *, runtime_request, approval):
    """Resolve the exact calling round, or only attachments for a call without a model run."""
    context = runtime_request.context
    session_id = uuid.UUID(str(context.session_id)) if context.session_id else None
    if context.runtime_task_id:
        run_id = uuid.UUID(str(context.runtime_task_id))
        statement = select(SessionModelResult, ChatSession).join(SessionToolInvocation,
            (SessionToolInvocation.provider_request_id == SessionModelResult.provider_request_id)
            & (SessionToolInvocation.round_id == SessionModelResult.round_id)).join(
                ChatSession, ChatSession.id == SessionModelResult.session_id
            ).options(load_only(SessionModelResult.id, SessionModelResult.state,
                SessionModelResult.model_request_hash, SessionModelResult.model_request_snapshot_json)).where(
                    SessionModelResult.tenant_id == context.tenant_id, SessionModelResult.run_id == run_id,
                    SessionModelResult.session_id == session_id, SessionToolInvocation.tenant_id == context.tenant_id,
                    SessionToolInvocation.session_id == session_id, SessionToolInvocation.run_id == run_id,
                    SessionToolInvocation.provider_tool_use_id == context.tool_call_id,
                    SessionToolInvocation.tool_name == runtime_request.tool_name,
                    SessionToolInvocation.effect_state == "prepared_not_started",
            )
        if approval is not None:
            statement = statement.where(SessionToolInvocation.effective_args_hash == approval.input_hash)
        else:
            statement = statement.where(SessionToolInvocation.result_event_id.is_(None))
        rows = (await db.execute(statement.limit(2))).all()
        if len(rows) != 1:
            raise ContentInputReceiptError('delegation_permission_sources_unavailable',
                'Restore the committed tool input inventory.', 503)
        model, session = rows[0]
        return session, model
    session = await db.get(ChatSession, session_id) if session_id is not None else None
    return session, None


async def validate_permission_input_sources(db, *, session, model, runtime_request):
    """An executor grant permits private work without granting outward disclosure."""
    from app.core.source_read_purpose import source_recipient_read
    from app.services.runtime_user_identity import load_scoped_runtime_user
    from app.services.source_read_queries import transcript_event_columns
    from app.tools.registry import tool_execution_policy

    context = runtime_request.context
    tenant_id = uuid.UUID(str(context.tenant_id))
    dependencies, complete = (model_output_dependencies(model) if context.authority_frame_required
        else _model_dependencies(model)) if model is not None else ([], not context.runtime_task_id)
    if not complete:
        raise ContentInputReceiptError('delegation_permission_sources_unavailable', 'Restore the committed tool input inventory.', 503)
    attachments = []
    prepared = None
    if runtime_request.tool_name == "send_email" and runtime_request.arguments.get("attachments"):
        from app.services.email_service import _prepare_email_attachments

        prepared = _prepare_email_attachments(context.workspace, runtime_request.arguments["attachments"],
            authority_scope=context.workspace_authority_scope, permission_profile=context.permission_profile)
        if isinstance(prepared, str):
            raise ContentInputReceiptError("content_source_access_denied", "This file cannot be attached.", 403)
        for target, content in prepared:
            receipt = await latest_workspace_receipt(db, tenant_id, context.agent_id,
                target.relative_to(context.workspace.resolve()).as_posix())
            if receipt is None or receipt.content_sha256 != hashlib.sha256(content).hexdigest():
                raise ContentInputReceiptError("workspace_content_unverified", "This file cannot be attached.")
            attachments.append({"source_kind": "workspace_artifact", "source_id": str(receipt.id),
                "source_version": receipt.content_sha256})
    if not dependencies and not attachments:
        return
    member = tool_execution_policy(runtime_request.tool_name).external_visible and session is not None and context.user_id is not None and (
        session.user_id == context.user_id and session.audience_kind != "group" or bool(attachments)
    )
    with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True) as scope:
        scope.share_session_source_facts = member
        transcript_event_columns(scope)
        sources = dependencies + attachments
        await _preload_execution_sources(db, tenant_id, sources)
        await _validate_input_dependencies(db, tenant_id, dependencies if member else sources,
            runtime_request=runtime_request)
        if member:
            user = scope.browser_reader
            if user is None or user.id != context.user_id:
                user = await load_scoped_runtime_user(db, context.user_id, tenant_id=tenant_id)
            with source_recipient_read(session_id=session.id, user_id=context.user_id, require_workspace_receipts=True):
                await _validate_input_dependencies(db, tenant_id, sources, user=user)
    if prepared is not None:
        context.prepared_email_attachments = prepared
