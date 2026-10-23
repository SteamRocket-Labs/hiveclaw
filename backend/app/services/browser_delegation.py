"""Browser resource authority on an otherwise read-only delegated session."""

from uuid import UUID

from fastapi import HTTPException

from app.core.execution_context import ExecutionPrincipal
from app.core.permissions import authorize_session_action
from app.models.runtime_task import RuntimeTask
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason, resolve_live_effect_principal
from app.services.session_v2_persistence import _validate_a2a_delegation_peer_binding


async def authorize_delegated_browser(db, source, decision, scope):
    from app.services.browser_control import BrowserControlError

    try:
        actor, delegation = await _validate_a2a_delegation_peer_binding(
            db, session=decision.session, agent=decision.agent,
            peer_agent_id=decision.session.peer_agent_id, delegation_runtime_task_id=decision.session.runtime_task_id,
        )
        if actor.id != source.user_id:
            raise PermissionError("browser_delegation_requester_changed")
        if scope is None:
            # The authenticated requester can still inspect or stop their
            # resource after parent execution authority has been withdrawn.
            return
        if delegation.status == "killed":
            raise PermissionError("browser_delegation_cancelled")
        if scope.runtime_task_id is not None:
            principal = source.execution_principal
            token = source.delegation_token
            if (
                not isinstance(principal, ExecutionPrincipal)
                or str(principal.tenant_id) != str(source.tenant_id)
                or principal.source_agent_id != source.agent_id
                or principal.requester_user_id != source.user_id
                or UUID(principal.root_session_id) != UUID(delegation.root_session_id)
                or (UUID(str(principal.root_runtime_task_id)) if principal.root_runtime_task_id else None)
                != delegation.root_runtime_task_id
                or tuple(principal.delegation_chain) != tuple(delegation.delegation_chain_json)
                or token is None or token.parent_agent_id != delegation.parent_agent_id
            ):
                raise PermissionError("browser_delegation_frame_changed")
            current = await db.get(RuntimeTask, scope.runtime_task_id, populate_existing=True)
            if current is None or current.tenant_id != source.tenant_id or current.root_user_id != source.user_id:
                raise PermissionError("browser_delegation_task_changed")
            if current.id != delegation.id and not (
                current.parent_agent_id == source.agent_id and current.parent_session_id == str(source.session_id)
                and (current.task_type == "a2a_continuation" and current.child_session_id == str(source.session_id)
                     and current.child_agent_id == source.agent_id and current.root_session_id == delegation.root_session_id
                     or current.task_type == "approval_execution" and source.approval_id is not None)
            ):
                raise PermissionError("browser_delegation_task_changed")
        child, agent = decision.session, decision.agent
        root_id = UUID(delegation.root_session_id)
        visited = set()
        while True:
            _, task = await _validate_a2a_delegation_peer_binding(
                db, session=child, agent=agent, peer_agent_id=child.peer_agent_id,
                delegation_runtime_task_id=child.runtime_task_id,
            )
            if task.id in visited or UUID(task.root_session_id) != root_id or task.root_user_id != source.user_id:
                raise PermissionError("browser_delegation_chain_changed")
            visited.add(task.id)
            parent = await authorize_session_action(
                db, actor, agent_id=task.parent_agent_id, session_id=UUID(task.parent_session_id),
                action="browser_delegation_source", require_execution=True, audit=False,
            )
            if (parent.session.audience_kind not in {None, "private"}
                or get_agent_lifecycle_block_reason(parent.agent) is not None
                or (await resolve_live_effect_principal(db, parent.agent)).block_reason is not None):
                raise PermissionError("browser_delegation_parent_revoked")
            if parent.session.id == root_id:
                break
            child, agent = parent.session, parent.agent
    except (PermissionError, ValueError, TypeError) as exc:
        raise BrowserControlError("browser_delegation_revoked", 403) from exc
    except HTTPException as exc:
        raise BrowserControlError(
            "browser_authority_unavailable" if exc.status_code >= 500 else "browser_delegation_revoked",
            503 if exc.status_code >= 500 else 403,
        ) from exc
