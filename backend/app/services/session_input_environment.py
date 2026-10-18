"""Trusted command-resource authority for an already-claimed input admission."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException

from app.models.chat_session import ChatSession
from app.models.external_principal import ExternalPrincipal
from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
from app.models.user import User
from app.services.execution_environments import EnvironmentCommandFrame, EnvironmentError
from app.services.external_principal_service import load_external_runtime_actor
from app.services.session_v2_persistence import AuthenticatedSessionAuthority, resolve_session_command_authority


@dataclass(frozen=True)
class AdmissionCommandBinding:
    authority: AuthenticatedSessionAuthority
    admission_id: uuid.UUID
    command_id: uuid.UUID
    input_id: uuid.UUID
    input_revision: int
    hook_run_id: uuid.UUID
    worker_id: str
    expected_user_id: uuid.UUID | None

    def receipt(self):
        return {
            "kind": "input_admission",
            "admission_id": str(self.admission_id),
            "command_id": str(self.command_id),
            "input_id": str(self.input_id),
            "hook_run_id": str(self.hook_run_id),
        }

    async def _rows(self, db, scope):
        admission = await db.get(SessionInputAdmission, self.admission_id, populate_existing=True)
        command = await db.get(SessionCommand, self.command_id, populate_existing=True)
        item = await db.get(SessionTurnInput, self.input_id, populate_existing=True)
        session = await db.get(ChatSession, self.authority.session_id, populate_existing=True)
        if (
            scope.tenant_id != self.authority.tenant_id
            or scope.user_id != self.expected_user_id
            or admission is None
            or command is None
            or item is None
            or session is None
            or admission.tenant_id != self.authority.tenant_id
            or command.tenant_id != self.authority.tenant_id
            or item.tenant_id != self.authority.tenant_id
            or session.tenant_id != self.authority.tenant_id
            or session.agent_id != self.authority.agent_id
            or admission.command_id != command.id
            or item.command_id != command.id
            or command.session_id != session.id
            or item.session_id != session.id
            or admission.input_id != item.id
            or admission.session_id != session.id
            or command.principal_type != self.authority.principal_type
            or command.principal_id != self.authority.principal_id
            or item.revision != self.input_revision
            or admission.input_revision != self.input_revision
            or admission.hook_run_id != self.hook_run_id
            or admission.state != "hook_running"
            or admission.lease_owner != self.worker_id
            or admission.lease_expires_at is None
            or admission.lease_expires_at <= datetime.now(UTC)
        ):
            raise EnvironmentError("environment_source_authority_revoked", 403)
        return command, session

    def _actor(self, actor):
        if (
            getattr(actor, "id", None) != self.expected_user_id
            or self.expected_user_id is None
            or not getattr(actor, "is_active", False)
            or self.authority.principal_type == "external_principal"
            and not getattr(actor, "authority_bound", False)
        ):
            raise EnvironmentError("environment_source_authority_revoked", 403)
        return actor

    async def _canonical(self, db, command, session):
        try:
            context = await resolve_session_command_authority(
                db, command=command, session=session, action=self.authority.action
            )
            if (
                context.authority.principal_type != self.authority.principal_type
                or context.authority.principal_id != self.authority.principal_id
            ):
                raise EnvironmentError("environment_source_authority_revoked", 403)
            return self._actor(context.actor)
        except HTTPException as exc:
            raise EnvironmentError(
                "environment_source_authority_unavailable"
                if exc.status_code >= 500
                else "environment_source_authority_revoked",
                503 if exc.status_code >= 500 else 403,
            ) from exc
        except (PermissionError, LookupError, RuntimeError) as exc:
            if isinstance(exc, EnvironmentError):
                raise
            raise EnvironmentError("environment_source_authority_revoked", 403) from exc

    async def authorize(self, db, scope):
        command, session = await self._rows(db, scope)
        # Canonical permissions, installation and group membership are checked
        # before each command, not repeatedly through a provider's network API.
        return await self._canonical(db, command, session)

    async def revalidate(self, db, scope):
        command, session = await self._rows(db, scope)
        from app.core.permissions import require_writable_session

        try:
            require_writable_session(session, action=self.authority.action)
            if self.authority.principal_type == "external_principal":
                actor = await load_external_runtime_actor(
                    db,
                    tenant_id=self.authority.tenant_id,
                    principal_id=self.authority.principal_id,
                    expected_user_id=self.expected_user_id,
                )
                if session.audience_kind != "group" and session.user_id != self.expected_user_id:
                    raise EnvironmentError("environment_source_authority_revoked", 403)
            else:
                actor = await db.get(User, self.expected_user_id, populate_existing=True)
                if actor is None:
                    # Reuse the bounded canonical platform-admin recovery path.
                    actor = await self._canonical(db, command, session)
            return self._actor(actor)
        except HTTPException as exc:
            raise EnvironmentError(
                "environment_source_authority_unavailable"
                if exc.status_code >= 500
                else "environment_source_authority_revoked",
                503 if exc.status_code >= 500 else 403,
            ) from exc
        except (PermissionError, LookupError, RuntimeError) as exc:
            if isinstance(exc, EnvironmentError):
                raise
            raise EnvironmentError("environment_source_authority_revoked", 403) from exc


async def admission_command_frame(db, *, authority, input_row, claim, worker_id):
    user_id = authority.principal_id if authority.principal_type == "user" else None
    if authority.principal_type == "external_principal":
        principal = await db.get(ExternalPrincipal, authority.principal_id, populate_existing=True)
        if principal is not None:
            actor = await load_external_runtime_actor(
                db, tenant_id=authority.tenant_id, principal_id=principal.id, expected_user_id=principal.linked_user_id
            )
            user_id = actor.id if actor.authority_bound else None
    binding = AdmissionCommandBinding(
        authority,
        claim.admission_id,
        input_row.command_id,
        input_row.id,
        input_row.revision,
        claim.hook_run_id,
        str(worker_id).strip(),
        user_id,
    )
    return EnvironmentCommandFrame(authority.tenant_id, authority.agent_id, user_id, None, binding)
