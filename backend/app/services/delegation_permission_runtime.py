"""Carry an authenticated native permission effect through its delegated frame."""

import uuid

from app.core.execution_context import A2AToolAuthorityFrame, ExecutionIdentity, ExecutionPrincipal
from app.models.runtime_task import RuntimeTask
from app.runtime.ccplus_contracts import build_permission_profile, permission_profile_snapshot, permission_profile_snapshot_hash
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.session_model_inputs import _hash, _model_dependencies


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
    _dependencies, complete = _model_dependencies(model)
    if not complete:
        raise ContentInputReceiptError('delegation_permission_sources_unavailable', 'Restore the committed tool input inventory.', 503)
    return {'authority_frame': frame, 'execution_identity': identity, 'delegation_token': token,
        'permission_profile': profile, 'budget_run_id': frame.budget_run_id,
        'runtime_task_id': frame.runtime_task_id, 'session_id': frame.session_id,
        't0_refs': (f'session-model-result:{model.id}',)}


async def validate_permission_input_sources(db, *, authority, session, invocation, model, execution):
    """Recheck captured sources at the shared permission effect boundary."""
    if not execution:
        from app.services.personal_knowledge_access import AgentRuntimePrincipal

        dependencies, _complete = _model_dependencies(model)
        principal = AgentRuntimePrincipal(agent_id=authority.agent_id, requester_user_id=authority.principal_id,
            session_id=str(session.id), runtime_task_id=str(invocation.run_id))
        await _validate_input_dependencies(db, authority.tenant_id, dependencies, runtime_request=principal)
        return
    from pathlib import Path
    from app.config import get_settings
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    frame = execution['authority_frame']
    context = ToolExecutionContext(agent_id=authority.agent_id, user_id=authority.principal_id,
        tenant_id=authority.tenant_id, workspace=Path(get_settings().AGENT_DATA_DIR) / str(authority.agent_id),
        session_id=str(session.id), runtime_task_id=str(invocation.run_id), tool_call_id=invocation.provider_tool_use_id,
        execution_principal=frame.principal, execution_identity=frame.execution_identity,
        delegation_token=frame.delegation_token, authority_frame_required=True,
        budget_run_id=frame.budget_run_id, permission_profile=frame.permission_profile)
    dependencies, complete = _model_dependencies(model)
    if not complete:
        raise ContentInputReceiptError('delegation_permission_sources_unavailable', 'Restore the committed tool input inventory.', 503)
    await _validate_input_dependencies(db, authority.tenant_id, dependencies,
        runtime_request=ToolExecutionRequest(tool_name=invocation.tool_name,
            arguments=invocation.effective_arguments_json, context=context))
