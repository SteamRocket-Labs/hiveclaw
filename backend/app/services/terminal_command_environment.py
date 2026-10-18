"""Command authority for a currently claimed canonical terminal delivery."""

import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from fastapi import HTTPException

from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.services.execution_environments import EnvironmentCommandFrame, EnvironmentError
from app.services.runtime_task_authority import runtime_task_requester_user_id
from app.services.runtime_user_identity import load_runtime_user
from app.services.runtime_terminal_boundary_outbox import (
    RuntimeTerminalBoundaryOutboxService,
    normalize_terminal_boundary_binding,
)


@dataclass(frozen=True)
class TerminalCommandBinding:
    item: object
    worker_id: str
    user_id: uuid.UUID | None
    external_principal_id: str | None
    command_id: str | None
    canonical_validator: object

    def receipt(self):
        return {
            "kind": "terminal_boundary",
            "boundary_id": str(self.item.id),
            "runtime_task_id": str(self.item.runtime_task_id),
            "attempt": self.item.attempt,
        }

    def owns_terminal_task(self, task):
        # _rows has already checked this exact delivery claim. A completed
        # RuntimeTask alone never grants a new command or revives its worker.
        return task.id == self.item.runtime_task_id and task.status == self.item.terminal_status

    async def _rows(self, db, scope):
        row = await db.get(RuntimeTerminalBoundaryOutbox, self.item.id, populate_existing=True)
        task = await db.get(RuntimeTask, self.item.runtime_task_id, populate_existing=True)
        if (
            scope.tenant_id != self.item.tenant_id
            or scope.user_id != self.user_id
            or row is None
            or row.tenant_id != self.item.tenant_id
            or row.status != "processing"
            or row.claimed_by != self.worker_id
            or row.lease_expires_at is None
            or row.lease_expires_at <= datetime.now(UTC)
            or row.claim_token != self.item.claim_token
            or RuntimeTerminalBoundaryOutboxService._claimed(row) != self.item
            or task is None
            or task.tenant_id != self.item.tenant_id
            or not self.owns_terminal_task(task)
            or task.root_user_id != self.user_id
            or str((task.metadata_json or {}).get("external_principal_id") or "")
            != str(self.external_principal_id or "")
            or str((task.metadata_json or {}).get("session_v2_command_id") or "") != str(self.command_id or "")
        ):
            raise EnvironmentError("environment_source_authority_revoked", 403)
        if (
            runtime_task_requester_user_id(
                {"id": task.id, "root_user_id": task.root_user_id, "metadata": task.metadata_json}
            )
            != self.user_id
        ):
            raise EnvironmentError("environment_source_authority_revoked", 403)
        return task

    def _actor(self, actor):
        if (
            actor is None
            or self.user_id is None
            or actor.id != self.user_id
            or not actor.is_active
            or self.external_principal_id is not None
            and not getattr(actor, "authority_bound", False)
        ):
            raise EnvironmentError("environment_source_authority_revoked", 403)
        return actor

    async def _identity(self, db, task, *, before_command):
        session = None
        try:
            session_id = uuid.UUID(self.item.session_id)
        except ValueError:
            pass  # Some direct invocations intentionally use a synthetic key.
        else:
            session = await db.get(ChatSession, session_id, populate_existing=True)
        if session is not None and session.audience_kind == "group" and before_command:
            from app.services.shared_group_authority import _load_shared_group_task_actor

            return self._actor(await _load_shared_group_task_actor(db, task=task, session=session))
        if self.command_id is not None and before_command:
            from app.models.session_v2 import SessionCommand
            from app.services.session_v2_persistence import resolve_session_command_authority

            command = await db.get(SessionCommand, uuid.UUID(self.command_id))
            if command is None or session is None:
                raise EnvironmentError("environment_source_authority_revoked", 403)
            context = await resolve_session_command_authority(
                db, command=command, session=session, action="mutate_session_input"
            )
            return self._actor(context.actor)
        if self.external_principal_id is not None:
            from app.services.external_principal_service import load_external_runtime_actor
            from app.services.session_v2_persistence import resolve_session_mutation_authority

            actor = self._actor(
                await load_external_runtime_actor(
                    db,
                    tenant_id=self.item.tenant_id,
                    principal_id=self.external_principal_id,
                    expected_user_id=self.user_id,
                )
            )
            if before_command:
                if session is None:
                    raise EnvironmentError("environment_source_authority_revoked", 403)
                await resolve_session_mutation_authority(
                    db,
                    user=actor,
                    agent_id=self.item.agent_id,
                    session_id=session.id,
                    action="mutate_session_input",
                    audit=False,
                )
            return actor
        return self._actor(await load_runtime_user(db, self.user_id))

    async def _validate(self, db, scope, *, before_command):
        try:
            task = await self._rows(db, scope)
            if before_command:
                RuntimeTerminalBoundaryOutboxService._validate_claim_integrity(self.item)
                canonical = normalize_terminal_boundary_binding(await self.canonical_validator(db, self.item))
                if canonical != normalize_terminal_boundary_binding(self.item.binding):
                    raise EnvironmentError("environment_source_authority_revoked", 403)
            return await self._identity(db, task, before_command=before_command)
        except HTTPException as exc:
            raise EnvironmentError(
                "environment_source_authority_unavailable"
                if exc.status_code >= 500
                else "environment_source_authority_revoked",
                503 if exc.status_code >= 500 else 403,
            ) from exc
        except (PermissionError, LookupError, ValueError, RuntimeError) as exc:
            if isinstance(exc, EnvironmentError):
                raise
            raise EnvironmentError("environment_source_authority_revoked", 403) from exc

    async def authorize(self, db, scope):
        return await self._validate(db, scope, before_command=True)

    async def revalidate(self, db, scope):
        # Remote source/membership checks belong before each command; lease
        # and live identity supervision here uses only the local database.
        return await self._validate(db, scope, before_command=False)


async def terminal_command_frame(db, *, item, worker_id, canonical_validator):
    task = await db.get(RuntimeTask, item.runtime_task_id, populate_existing=True)
    user_id = task.root_user_id if task is not None else None
    metadata = task.metadata_json or {} if task is not None else {}
    binding = TerminalCommandBinding(
        replace(item, binding=deepcopy(item.binding)),
        str(worker_id),
        user_id,
        str(metadata.get("external_principal_id") or "") or None,
        str(metadata.get("session_v2_command_id") or "") or None,
        canonical_validator,
    )
    return EnvironmentCommandFrame(item.tenant_id, item.agent_id, user_id, item.runtime_task_id, binding)
