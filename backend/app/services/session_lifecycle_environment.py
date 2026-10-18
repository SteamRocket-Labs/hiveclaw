"""Trusted subscriber references and current authority for lifecycle commands."""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.runtime.hooks import HookEvent
from app.services.execution_environments import EnvironmentCommandFrame, EnvironmentError
from app.services.runtime_user_identity import load_runtime_user

ORIGIN_SCHEMA = "hive.session.lifecycle-origin.v1"
_EVENTS = {HookEvent.SESSION_IDLE, HookEvent.SESSION_CLOSE}


@dataclass(frozen=True)
class SessionLifecycleOrigin:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    user_id: uuid.UUID
    event: HookEvent

    def reference(self):
        return {
            "schema": ORIGIN_SCHEMA,
            **{key: str(getattr(self, key)) for key in ("tenant_id", "agent_id", "session_id", "user_id", "event")},
        }

    @classmethod
    def from_reference(cls, value):
        try:
            if not isinstance(value, dict) or value.get("schema") != ORIGIN_SCHEMA:
                raise ValueError
            event = HookEvent(value["event"])
            if event not in _EVENTS:
                raise ValueError
            return cls(
                *(uuid.UUID(str(value[key])) for key in ("tenant_id", "agent_id", "session_id", "user_id")), event
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise EnvironmentError("lifecycle_origin_required", 503) from exc


@dataclass(frozen=True)
class SessionLifecycleBinding:
    origin: SessionLifecycleOrigin

    def receipt(self):
        return {"kind": "session_lifecycle", "session_id": str(self.origin.session_id), "event": str(self.origin.event)}

    def frame(self):
        origin = self.origin
        return EnvironmentCommandFrame(origin.tenant_id, origin.agent_id, origin.user_id, None, self)

    async def _rows(self, db, scope=None):
        origin = self.origin
        actor = await load_runtime_user(db, origin.user_id)
        session = await db.get(ChatSession, origin.session_id, populate_existing=True)
        tenant = await db.get(Tenant, origin.tenant_id, populate_existing=True)
        if (
            actor is None
            or actor.id != origin.user_id
            or not actor.is_active
            or actor.tenant_id != origin.tenant_id
            and actor.role != "platform_admin"
            or session is None
            or session.tenant_id != origin.tenant_id
            or session.agent_id != origin.agent_id
            or session.source_channel == "agent"
            or session.session_kind == "agent_chat"
            or tenant is None
            or not tenant.is_active
            or scope is not None
            and (scope.tenant_id != origin.tenant_id or scope.user_id != origin.user_id)
        ):
            raise EnvironmentError("environment_source_authority_revoked", 403)
        return actor, session

    async def _authorize(self, db, scope=None, *, execution=False, remote=True):
        from app.core.permissions import (
            authorize_session_action,
            check_agent_access,
            reject_non_executing_agent_access,
            require_writable_session,
        )

        try:
            actor, session = await self._rows(db, scope)
            if session.audience_kind == "group":
                if execution:
                    if remote:
                        from app.services.shared_group_authority import resolve_shared_group_web_authority

                        await resolve_shared_group_web_authority(
                            db, session=session, user=actor, action="mutate_session_input", audit=False
                        )
                    else:
                        _, access = await check_agent_access(db, actor, session.agent_id)
                        reject_non_executing_agent_access(access)
                        require_writable_session(session, action="session_lifecycle_command")
                else:
                    from app.services.shared_group_history import authorize_group_history_reader

                    await authorize_group_history_reader(db, session=session, user=actor, audit=False)
            else:
                await authorize_session_action(
                    db,
                    actor,
                    agent_id=self.origin.agent_id,
                    session_id=self.origin.session_id,
                    action="session_lifecycle_command" if execution else "session_lifecycle_read",
                    require_writable=execution,
                    require_execution=execution,
                    audit=False,
                )
            return actor, session
        except HTTPException as exc:
            raise EnvironmentError(
                "environment_source_authority_unavailable"
                if exc.status_code >= 500
                else "environment_source_authority_revoked",
                503 if exc.status_code >= 500 else 403,
            ) from exc

    async def authorize(self, db, scope):
        actor, _ = await self._authorize(db, scope, execution=True)
        return actor

    async def revalidate(self, db, scope):
        actor, _ = await self._authorize(db, scope, execution=True, remote=False)
        return actor

    async def messages(self, db, *, limit):
        _, session = await self._authorize(db)
        if session.audience_kind == "group":
            # The authenticated subscriber deliberately has no raw group
            # conversation. Durable memory reads the existing T0/T2 sources.
            return []
        rows = list(
            await db.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.tenant_id == self.origin.tenant_id,
                    ChatMessage.agent_id == self.origin.agent_id,
                    ChatMessage.conversation_id == str(self.origin.session_id),
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(max(1, limit))
            )
        )
        rows.reverse()
        return [{"role": row.role, "content": row.content} for row in rows if row.content is not None]
