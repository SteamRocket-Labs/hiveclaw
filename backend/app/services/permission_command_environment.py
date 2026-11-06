"""Command ownership for one exclusively held, applied session permission."""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException

from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from app.services.execution_environments import EnvironmentError, current_command_frame
from app.services.permission_execution import PermissionExecutionLease, PermissionExecutionUnavailable
from app.services.runtime_task_authority import runtime_task_requester_user_id
from app.services.runtime_user_identity import load_runtime_user
from app.services.session_v2_persistence import AuthenticatedSessionAuthority, resolve_session_mutation_authority
from app.services.shared_group_authority import group_permission_execution_matches, _permission_execution_matches


@dataclass(frozen=True)
class PermissionCommandBinding:
    authority: AuthenticatedSessionAuthority
    invocation_id: uuid.UUID
    run_id: uuid.UUID
    permission_request_id: uuid.UUID
    tool_call_id: str
    lease: PermissionExecutionLease

    def receipt(self):
        return {"kind": "session_permission", "permission_request_id": str(self.permission_request_id),
            "invocation_id": str(self.invocation_id), "runtime_task_id": str(self.run_id)}

    def owns_terminal_task(self, task):
        metadata = task.metadata_json or {}
        try:
            requester = runtime_task_requester_user_id({"id": task.id, "root_user_id": task.root_user_id,
                "metadata": task.metadata_json})
        except ValueError:
            return False
        return bool(
            self.lease.acquired and not self.lease.connection.closed and not self.lease.connection.invalidated
            and task.id == self.run_id and task.status == "suspended"
            and metadata.get("cancel_state") != "cancelling"
            and requester == self.authority.principal_id
            and metadata.get("interactive_pause") == "awaiting_session_permission"
            and group_permission_execution_matches(self.authority, self.invocation_id, self.run_id)
        )

    async def _validate(self, db, scope, *, before_command):
        try:
            await self.lease.ensure_owned()
            task = await db.get(RuntimeTask, self.run_id, populate_existing=True)
            invocation = await db.get(SessionToolInvocation, self.invocation_id, populate_existing=True)
            session = await db.get(ChatSession, self.authority.session_id, populate_existing=True)
            frame = current_command_frame()
            if (
                any(str(getattr(scope, key)) != str(value) for key, value in (
                    ("tenant_id", self.authority.tenant_id), ("agent_id", self.authority.agent_id),
                    ("user_id", self.authority.principal_id), ("runtime_task_id", self.run_id),
                    ("session_id", self.authority.session_id)))
                or task is None or not self.owns_terminal_task(task)
                or session is None or scope.runtime_request is None
                or frame is None or frame.tool_call_id != self.tool_call_id
                or invocation is None or invocation.permission_item_id != self.permission_request_id
                or invocation.permission_item_id != self.lease.permission_request_id
                or invocation.effect_state != "effect_started" or invocation.result_event_id is not None
                or not await _permission_execution_matches(db, task=task, session=session, runtime_request=scope.runtime_request)
            ):
                raise EnvironmentError("environment_source_authority_revoked", 403)
            actor = await load_runtime_user(db, self.authority.principal_id)
            if actor is None or not actor.is_active:
                raise EnvironmentError("environment_source_authority_revoked", 403)
            if before_command:
                await resolve_session_mutation_authority(db, user=actor, agent_id=self.authority.agent_id,
                    session_id=self.authority.session_id, action="respond_tool_permission", audit=False)
            return actor
        except (PermissionExecutionUnavailable, HTTPException, ValueError, PermissionError) as exc:
            unavailable = isinstance(exc, PermissionExecutionUnavailable) or isinstance(exc, HTTPException) and exc.status_code >= 500
            raise EnvironmentError("environment_source_authority_unavailable" if unavailable
                else "environment_source_authority_revoked", 503 if unavailable else 403) from exc

    async def authorize(self, db, scope):
        return await self._validate(db, scope, before_command=True)

    async def revalidate(self, db, scope):
        return await self._validate(db, scope, before_command=False)
