"""Independent machine actors derived only from durable task and credential authority."""
from dataclasses import dataclass
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.chat_session import ChatSession
from app.models.external_principal import ExternalPrincipal
from app.models.runtime_task import RuntimeTask
from app.services.machine_identity_service import MachineAuthContext, load_machine_grant
from app.services.personal_knowledge_access import AgentRuntimePrincipal


class MachineRuntimeAuthorityDenied(PermissionError):
    """Current machine authority no longer permits a new governed effect."""


@dataclass(frozen=True, slots=True)
class MachineArtifactReader(AgentRuntimePrincipal):
    """Server-built source reader for an existing task's captured output."""


@dataclass(frozen=True, slots=True)
class MachineRuntimeActor:
    external_principal_id: uuid.UUID
    machine_credential_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    display_name: str
    id: None = None
    role: str = 'machine'
    department_id: None = None
    authority_bound: bool = False
    is_active: bool = True

    @property
    def username(self):
        return f'a2a:{self.external_principal_id}'


async def machine_runtime_actor(db, context: MachineAuthContext):
    principal = await db.get(ExternalPrincipal, context.principal_id, populate_existing=True)
    return MachineRuntimeActor(context.principal_id, context.credential_id, context.tenant_id,
        context.agent_id, principal.display_name)


async def load_machine_runtime_actor(db, *, task: RuntimeTask, session: ChatSession, scope='agent.execute'):
    if (task is None or session is None or task.machine_credential_id is None or task.root_external_principal_id is None
        or task.root_user_id is not None or session.user_id is not None
        or session.external_principal_id != task.root_external_principal_id
        or task.parent_agent_id != session.agent_id or task.tenant_id != session.tenant_id
        or task.parent_session_id != str(session.id)):
        raise HTTPException(403, detail={'code': 'machine_task_authority_mismatch'})
    context, _agent = await load_machine_grant(db, credential_id=task.machine_credential_id,
        agent_id=task.parent_agent_id, principal_id=task.root_external_principal_id, scope=scope)
    return await machine_runtime_actor(db, context)


def is_machine_runtime_actor(actor):
    return isinstance(actor, MachineRuntimeActor)


async def machine_task_actor(db, *, tenant_id, agent_id, runtime_task_id, session_id, user_id, scope='agent.execute'):
    """Return None for a nonmachine run; never reinterpret a machine as a User."""
    if not runtime_task_id:
        return None
    task = await db.get(RuntimeTask, uuid.UUID(str(runtime_task_id)), populate_existing=True)
    if task is None or task.machine_credential_id is None:
        return None
    if (str(task.tenant_id) != str(tenant_id) or task.parent_agent_id != agent_id or user_id is not None
        or str(task.parent_session_id) != str(session_id)):
        raise HTTPException(403, detail={'code': 'machine_task_authority_mismatch'})
    session = await db.get(ChatSession, uuid.UUID(str(session_id)), populate_existing=True)
    if session is None:
        raise HTTPException(403, detail={'code': 'machine_task_session_unavailable'})
    return await load_machine_runtime_actor(db, task=task, session=session, scope=scope)


async def machine_request_actor(db, tenant_id, request):
    context = getattr(request, 'context', None) or request
    session = getattr(context, 'session_context', None)
    metadata = getattr(session, 'metadata', None) or {}
    return await machine_task_actor(db, tenant_id=tenant_id, agent_id=context.agent_id,
        runtime_task_id=getattr(context, 'runtime_task_id', None) or metadata.get('runtime_task_id') or metadata.get('task_id'),
        session_id=getattr(context, 'session_id', None) or getattr(session, 'session_id', None),
        user_id=getattr(context, 'user_id', getattr(context, 'requester_user_id', None)),
        scope='task.read' if isinstance(request, MachineArtifactReader) else 'agent.execute')


async def revalidate_machine_tool_context(db, context):
    actor = await machine_request_actor(db, context.tenant_id, context)
    if (actor is None or actor.machine_credential_id != context.machine_credential_id
        or actor.external_principal_id != context.external_principal_id):
        raise HTTPException(403, detail={'code': 'machine_task_authority_mismatch'})


async def machine_approval_origin(db, *, session, approval):
    """Bind historical mechanical facts without authorizing another effect."""
    from app.models.machine_credential import MachineCredential
    from app.services.approval_ticket import ApprovalTicketError, restore_approval_execution_envelope
    from app.services.session_approval import group_approval_origin

    envelope = restore_approval_execution_envelope(approval.execution_envelope,
        expected_hash=approval.execution_envelope_hash)
    task, _invocation, _event = await group_approval_origin(db, session=session, approval=approval)
    credential = await db.get(MachineCredential, task.machine_credential_id, populate_existing=True) if task.machine_credential_id else None
    if (approval.requested_by is not None or envelope.requester_user_id is not None
        or credential is None or credential.tenant_id != session.tenant_id or credential.agent_id != session.agent_id
        or task.tenant_id != session.tenant_id or task.parent_agent_id != session.agent_id
        or task.parent_session_id != str(session.id) or task.root_user_id is not None or session.user_id is not None
        or envelope.machine_credential_id != task.machine_credential_id
        or credential.principal_id != task.root_external_principal_id
        or session.external_principal_id != task.root_external_principal_id
        or envelope.requester_external_principal_id != task.root_external_principal_id
        or approval.requested_by_external_principal_id != task.root_external_principal_id):
        raise ApprovalTicketError('machine approval origin authority mismatch')
    return task


async def machine_approval_actor(db, *, session, approval):
    from app.services.approval_ticket import ApprovalTicketError

    task = await machine_approval_origin(db, session=session, approval=approval)
    actor = await load_machine_runtime_actor(db, task=task, session=session)
    if task.status == 'killed':
        raise ApprovalTicketError('machine approval origin was cancelled')
    return task, actor


async def machine_approval_return_actor(db, *, session, rows, root_runtime_task_id):
    """Revalidate the exact durable ticket, canonical origin, and result route."""
    from app.services.session_approval import load_session_approval

    requester = None
    for row in rows:
        try:
            approval_id = uuid.UUID(str((row.metadata_json or {}).get('approval_id')))
        except (ValueError, TypeError) as exc:
            raise PermissionError('machine_approval_return_source_mismatch') from exc
        approval = await load_session_approval(db, session=session, approval_id=approval_id)
        if (approval is None or row.source_kind != 'approval' or row.tenant_id != session.tenant_id
            or row.parent_agent_id != session.agent_id or row.parent_session_id != session.id
            or row.parent_user_id is not None or row.root_runtime_task_id != root_runtime_task_id
            or str(row.source_run_id) != str(approval.execution_task_id or approval.id)
            or (approval.execution_receipt or {}).get('continuation_outbox_id') != str(row.id)):
            raise PermissionError('machine_approval_return_source_mismatch')
        original, actor = await machine_approval_actor(db, session=session, approval=approval)
        if (original.root_runtime_task_id or original.id) != root_runtime_task_id:
            raise PermissionError('machine_approval_return_root_mismatch')
        if approval.execution_task_id is not None:
            job = await db.get(RuntimeTask, approval.execution_task_id, populate_existing=True)
            if (job is None or job.tenant_id != session.tenant_id or job.parent_agent_id != session.agent_id
                or job.root_user_id is not None or job.root_external_principal_id != actor.external_principal_id
                or job.machine_credential_id != actor.machine_credential_id or job.parent_session_id != str(session.id)):
                raise PermissionError('machine_approval_return_execution_mismatch')
        if requester is not None and requester != actor:
            raise PermissionError('machine_approval_return_requester_mismatch')
        requester = actor
    if requester is None:
        raise PermissionError('machine_approval_return_source_missing')
    return requester


async def load_machine_workspace_scope(db, *, actor, session_id):
    from app.models.workspace_resource import WorkspaceResourceManifest
    session = await db.get(ChatSession, session_id, populate_existing=True)
    if (session is None or session.tenant_id != actor.tenant_id or session.agent_id != actor.agent_id
        or session.external_principal_id != actor.external_principal_id or session.user_id is not None):
        raise HTTPException(403, detail={'code': 'machine_task_session_unavailable'})
    root_id = session.root_session_id or session.id
    manifests = list(await db.scalars(select(WorkspaceResourceManifest).where(
        WorkspaceResourceManifest.tenant_id == actor.tenant_id, WorkspaceResourceManifest.agent_id == actor.agent_id)))
    allowed = frozenset(row.path for row in manifests if row.authority_state == 'owned' and row.deleted_at is None
        and row.owner_user_id is None and row.root_session_id == root_id)
    return MachineWorkspaceAuthorityScope(agent_id=actor.agent_id, user_id=None, root_session_id=root_id,
        allowed_paths=allowed, operator_view=False, authority_source='machine_work_session',
        known_paths=frozenset(row.path for row in manifests),
        action_permissions=frozenset((path, action) for path in allowed for action in ('read', 'write', 'delete', 'deliver')),
        session_source_ids=frozenset({str(session.id)}))


from app.services.workspace_resource_authority import WorkspaceAuthorityScope  # noqa: E402


@dataclass(frozen=True, slots=True)
class MachineWorkspaceAuthorityScope(WorkspaceAuthorityScope):
    def can_read(self, path):
        if not path.startswith('workspace/') and path != 'workspace':
            return path in {'soul.md', 'memory/self/self.md'} or path.startswith('skills/')
        return super(MachineWorkspaceAuthorityScope, self).can_read(path)

    def can_perform(self, path, action):
        if not path.startswith('workspace/') and path != 'workspace':
            return action == 'read' and self.can_read(path)
        return super(MachineWorkspaceAuthorityScope, self).can_perform(path, action)
