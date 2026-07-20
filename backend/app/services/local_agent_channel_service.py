"""Local Agent Channel service.

This layer turns a bound local bridge connection into an IM-like channel:
browser/A2A messages are durably queued, the local runner consumes them over
WebSocket or fallback poll, and runner events are replayable through Hive
ChatSession plus local channel event rows.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, ChatMessage
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.local_agent_channel import (
    LocalAgentCapabilitySnapshot,
    LocalAgentChannel,
    LocalAgentChannelEvent,
    LocalAgentChannelMessage,
    LocalAgentChannelSession,
    LocalAgentChannelWsTicket,
)
from app.models.local_bridge import LocalAgentBridgeConnection
from app.services.chat_artifact_delivery import create_or_bind_chat_session
from app.services.local_agent_protocol import (
    build_execution_receipt,
    build_signed_capability_snapshot,
    effective_local_capabilities,
    verify_capability_snapshot,
)
from app.services.local_bridge_service import BridgeAuthContext, hash_secret, utcnow

WS_TICKET_PREFIX = "hbt_"
BROWSER_WS_TICKET_PREFIX = "hbwt_"
DEFAULT_WS_TICKET_SECONDS = 60
DEFAULT_BROWSER_WS_TICKET_SECONDS = 60
DEFAULT_CAPABILITY_SNAPSHOT_SECONDS = 24 * 60 * 60
DEFAULT_DELIVERY_LEASE_SECONDS = 60
DEFAULT_DELIVERY_ACTIVITY_LEASE_SECONDS = 5 * 60
DEFAULT_MAX_DELIVERY_ATTEMPTS = 5

LOCAL_CAPABILITY_SCOPE_REQUIREMENTS: dict[str, frozenset[str]] = {
    "execute": frozenset({"local_agent:receive", "local_agent:report"}),
    "event_stream": frozenset({"local_agent:send"}),
    "result_report": frozenset({"local_agent:report"}),
    "file_download": frozenset({"local_agent:receive"}),
    "file_upload": frozenset({"local_agent:report"}),
}

settings = get_settings()
MAX_DELIVERY_ATTEMPTS = max(
    1,
    int(getattr(settings, "LOCAL_AGENT_DELIVERY_MAX_ATTEMPTS", DEFAULT_MAX_DELIVERY_ATTEMPTS)),
)


def local_capability_signing_secret() -> str:
    """Derive a purpose-separated signing secret from the server secret."""

    raw = f"{settings.JWT_SECRET_KEY}:hive.local-capability-snapshot.v1"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def capability_snapshot_payload(snapshot: LocalAgentCapabilitySnapshot) -> dict[str, Any]:
    return {
        "schema": "hive.local_capability_snapshot.v1",
        "issuer": snapshot.issuer,
        "subject_agent_id": str(snapshot.subject_agent_id),
        "tenant_id": str(snapshot.tenant_id),
        "scopes": list(snapshot.effective_capabilities_json or []),
        "issued_at": snapshot.issued_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
        "version": snapshot.version,
        "snapshot_hash": snapshot.snapshot_hash,
        "signature": snapshot.signature,
    }


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reported_capability_names(capabilities: dict[str, Any] | None) -> tuple[str, ...]:
    payload = dict(capabilities or {})
    reported = {
        str(name).strip()
        for name, enabled in payload.items()
        if name in LOCAL_CAPABILITY_SCOPE_REQUIREMENTS and enabled is True
    }
    nested = payload.get("capabilities")
    if isinstance(nested, list):
        reported.update(
            str(name).strip() for name in nested if str(name).strip() in LOCAL_CAPABILITY_SCOPE_REQUIREMENTS
        )
    return tuple(sorted(reported))


def _server_capability_names(context: BridgeAuthContext) -> tuple[str, ...]:
    scopes = set(context.scopes or ())
    return tuple(
        sorted(
            capability
            for capability, requirements in LOCAL_CAPABILITY_SCOPE_REQUIREMENTS.items()
            if requirements.issubset(scopes)
        )
    )


def _receipt_for_message(
    message: LocalAgentChannelMessage,
    *,
    result_refs: list[str] | None = None,
    receipt_status: str | None = None,
) -> dict[str, Any] | None:
    request_hash = str(getattr(message, "request_hash", None) or "")
    snapshot_hash = str(getattr(message, "capability_snapshot_hash", None) or "")
    replay_key = str(getattr(message, "replay_key", None) or "")
    trace_id = str(getattr(message, "receipt_trace_id", None) or "")
    span_id = str(getattr(message, "receipt_span_id", None) or "")
    if not all((request_hash, snapshot_hash, replay_key, trace_id, span_id)):
        return None
    metadata = dict(getattr(message, "metadata_json", None) or {})
    stored = metadata.get("execution_receipt")
    stored_result_refs = list(stored.get("result_refs") or []) if isinstance(stored, dict) else []
    return build_execution_receipt(
        request_hash=request_hash,
        capability_snapshot_hash=snapshot_hash,
        result_refs=result_refs if result_refs is not None else stored_result_refs,
        status=str(receipt_status or getattr(message, "status", "pending")),
        replay_key=replay_key,
        trace_id=trace_id,
        span_id=span_id,
    )


def _generate_ws_ticket() -> str:
    return f"{WS_TICKET_PREFIX}{secrets.token_urlsafe(32)}"


def _parse_ws_ticket(ticket: str) -> str:
    normalized = (ticket or "").strip()
    if not normalized.startswith(WS_TICKET_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid local agent channel ticket")
    return normalized


def _parse_browser_ws_ticket(ticket: str) -> str:
    normalized = (ticket or "").strip()
    if not normalized.startswith(BROWSER_WS_TICKET_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid local agent browser ticket")
    return normalized[len(BROWSER_WS_TICKET_PREFIX) :]


def _require_scope(context: BridgeAuthContext, *accepted_scopes: str) -> None:
    scopes = set(context.scopes or ())
    if not scopes.intersection(accepted_scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Bridge token lacks local agent channel scope"
        )


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _event_payload(event: LocalAgentChannelEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "sequence": int(getattr(event, "sequence", 0) or 0),
        "session_id": str(event.session_id),
        "message_id": str(event.message_id) if event.message_id else None,
        "direction": event.direction,
        "type": event.event_type,
        "payload": dict(event.payload_json or {}),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _message_payload(message: LocalAgentChannelMessage) -> dict[str, Any]:
    payload = {
        "id": str(message.id),
        "session_id": str(message.session_id),
        "owner_user_id": str(message.owner_user_id),
        "source_agent_id": str(message.source_agent_id) if message.source_agent_id else None,
        "tenant_id": str(message.tenant_id) if message.tenant_id else None,
        "direction": message.direction,
        "content": message.content,
        "attachments": list(message.attachments_json or []),
        "metadata": dict(message.metadata_json or {}),
        "idempotency_key": str(getattr(message, "idempotency_key", "") or ""),
        "request_hash": getattr(message, "request_hash", None),
        "capability_snapshot_hash": getattr(message, "capability_snapshot_hash", None),
        "replay_key": str(getattr(message, "replay_key", "") or ""),
        "approval_id": str(message.approval_id) if getattr(message, "approval_id", None) else None,
        "status": message.status,
        "delivery_attempt_count": int(getattr(message, "delivery_attempt_count", 0) or 0),
        "delivery_lease_expires_at": (
            message.delivery_lease_expires_at.isoformat()
            if getattr(message, "delivery_lease_expires_at", None)
            else None
        ),
        "result": message.result,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
        "completed_at": message.completed_at.isoformat() if message.completed_at else None,
    }
    receipt = _receipt_for_message(message)
    if receipt is not None:
        payload["receipt"] = receipt
    return payload


def _session_payload(session: LocalAgentChannelSession, chat_session: ChatSession | None = None) -> dict[str, Any]:
    title = getattr(chat_session, "title", None) or "Local Agent"
    last_message_at = getattr(chat_session, "last_message_at", None)
    updated_at = last_message_at or getattr(session, "updated_at", None) or getattr(session, "created_at", None)
    return {
        "id": session.id,
        "chat_session_id": session.chat_session_id,
        "agent_id": session.source_agent_id,
        "title": title,
        "source": session.source,
        "source_channel": getattr(chat_session, "source_channel", None) or "local_agent",
        "session_kind": getattr(chat_session, "session_kind", None) or "local_agent_channel",
        "status": session.status,
        "created_at": session.created_at,
        "updated_at": updated_at,
        "last_message_at": last_message_at,
    }


def _local_agent_external_conversation_id(
    *,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    source_agent_id: uuid.UUID,
    source: str,
) -> str:
    if actor_user_id == owner_user_id:
        return f"local_agent:{owner_user_id}:{source_agent_id}:{source}"
    return f"local_agent:{owner_user_id}:{source_agent_id}:{actor_user_id}:{source}"


async def create_ws_ticket(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    ttl_seconds: int = DEFAULT_WS_TICKET_SECONDS,
) -> dict[str, Any]:
    """Create a short-lived single-use ticket for the local runner WebSocket."""

    _require_scope(context, "local_agent:connect")
    raw_ticket = _generate_ws_ticket()
    row = LocalAgentChannelWsTicket(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        connection_id=context.connection_id,
        ticket_hash=hash_secret(raw_ticket),
        scopes=list(context.scopes),
        metadata_json={"client_kind": context.client_kind, "device_name": context.device_name},
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
    )
    db.add(row)
    await db.flush()
    await db.commit()
    return {"ticket": raw_ticket, "expires_in": ttl_seconds, "single_use": True}


async def resolve_ws_ticket(
    db: AsyncSession,
    *,
    ticket: str,
    user_agent: str | None = None,
    last_seen_ip: str | None = None,
) -> BridgeAuthContext:
    """Resolve and consume a WebSocket ticket."""

    raw_ticket = _parse_ws_ticket(ticket)
    async with enter_rls_bypass(db, reason="local agent channel ws ticket lookup"):
        result = await db.execute(
            select(LocalAgentChannelWsTicket).where(
                LocalAgentChannelWsTicket.ticket_hash == hash_secret(raw_ticket),
                LocalAgentChannelWsTicket.consumed_at.is_(None),
            )
        )
        row = result.scalar_one_or_none()
        connection = await db.get(LocalAgentBridgeConnection, row.connection_id) if row is not None else None
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid local agent channel ticket")
    if (
        connection is None
        or connection.status != "active"
        or connection.tenant_id != row.tenant_id
        or connection.user_id != row.user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Local agent bridge connection is inactive"
        )
    if connection.expires_at is None or connection.expires_at <= utcnow():
        if connection.expires_at is not None:
            connection.status = "expired"
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Local agent bridge connection has expired",
        )
    if row.expires_at <= utcnow():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Local agent channel ticket expired")
    row.consumed_at = utcnow()
    row.metadata_json = {
        **(row.metadata_json or {}),
        "last_seen_ip": last_seen_ip,
        "user_agent": user_agent,
    }
    await pin_rls_tenant_context(db, row.tenant_id)
    await db.commit()
    return BridgeAuthContext(
        connection_id=row.connection_id,
        tenant_id=row.tenant_id,
        agent_id=connection.agent_id,
        user_id=row.user_id,
        scopes=tuple(row.scopes or []),
        client_kind=str((row.metadata_json or {}).get("client_kind") or "local_agent"),
        device_name=str((row.metadata_json or {}).get("device_name") or "Local Agent"),
    )


async def get_channel_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> dict[str, Any]:
    result = await db.execute(
        select(LocalAgentChannelSession).where(
            LocalAgentChannelSession.id == session_id,
            LocalAgentChannelSession.owner_user_id == owner_user_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel session not found")
    return _session_payload(session)


async def get_channel_session_for_actor(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[dict[str, Any], uuid.UUID]:
    """Resolve a channel session visible to a browser actor.

    The channel row remains owned by the host user whose runner consumes work.
    A shared caller sees the session through its mirrored ChatSession.user_id.
    """

    result = await db.execute(
        select(LocalAgentChannelSession, ChatSession)
        .outerjoin(ChatSession, ChatSession.id == LocalAgentChannelSession.chat_session_id)
        .where(
            LocalAgentChannelSession.id == session_id,
            LocalAgentChannelSession.status == "active",
            or_(
                LocalAgentChannelSession.owner_user_id == actor_user_id,
                ChatSession.user_id == actor_user_id,
            ),
        )
        .limit(1)
    )
    rows = result.all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel session not found")
    session, chat_session = rows[0]
    return _session_payload(session, chat_session), session.owner_user_id


def _tenant_filter(column, tenant_id: uuid.UUID | None):
    if tenant_id is None:
        return column.is_(None)
    return column == tenant_id


async def list_agent_channel_sessions(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    source_agent_id: uuid.UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List active local-channel sessions for one local Agent row."""

    stmt = (
        select(LocalAgentChannelSession, ChatSession)
        .outerjoin(ChatSession, ChatSession.id == LocalAgentChannelSession.chat_session_id)
        .where(
            _tenant_filter(LocalAgentChannelSession.tenant_id, tenant_id),
            LocalAgentChannelSession.owner_user_id == owner_user_id,
            LocalAgentChannelSession.source_agent_id == source_agent_id,
            LocalAgentChannelSession.status == "active",
        )
        .order_by(
            ChatSession.last_message_at.desc().nulls_last(),
            LocalAgentChannelSession.created_at.desc(),
            LocalAgentChannelSession.id.desc(),
        )
        .limit(limit)
    )
    if actor_user_id is not None:
        stmt = stmt.where(ChatSession.user_id == actor_user_id)
    result = await db.execute(stmt)
    return [_session_payload(session, chat_session) for session, chat_session in result.all()]


async def resolve_agent_channel_session(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    source_agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> dict[str, Any]:
    """Resolve either a LocalAgentChannelSession id or its mirrored ChatSession id."""

    session, chat_session = await _resolve_agent_channel_session_row(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        actor_user_id=actor_user_id,
        source_agent_id=source_agent_id,
        session_id=session_id,
    )
    return _session_payload(session, chat_session)


async def _resolve_agent_channel_session_row(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    source_agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> tuple[LocalAgentChannelSession, ChatSession | None]:
    stmt = (
        select(LocalAgentChannelSession, ChatSession)
        .outerjoin(ChatSession, ChatSession.id == LocalAgentChannelSession.chat_session_id)
        .where(
            _tenant_filter(LocalAgentChannelSession.tenant_id, tenant_id),
            LocalAgentChannelSession.owner_user_id == owner_user_id,
            LocalAgentChannelSession.source_agent_id == source_agent_id,
            LocalAgentChannelSession.status == "active",
            or_(
                LocalAgentChannelSession.id == session_id,
                LocalAgentChannelSession.chat_session_id == session_id,
            ),
        )
        .limit(1)
    )
    if actor_user_id is not None:
        stmt = stmt.where(ChatSession.user_id == actor_user_id)
    result = await db.execute(stmt)
    rows = result.all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel session not found")
    return rows[0]


async def archive_agent_channel_session(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    source_agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> dict[str, Any]:
    """Archive one local-channel session without deleting its replayable event history."""

    session, chat_session = await _resolve_agent_channel_session_row(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        actor_user_id=actor_user_id,
        source_agent_id=source_agent_id,
        session_id=session_id,
    )
    session.status = "archived"
    if chat_session is not None:
        chat_session.listed_surface = "archived"
        if getattr(chat_session, "external_conv_id", None):
            chat_session.external_conv_id = f"{chat_session.external_conv_id}#archived:{session.id.hex[:12]}"
    await db.commit()
    return {"status": "archived"}


async def get_or_create_default_channel_session(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    source_agent_id: uuid.UUID | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Return the durable default Local Agent Channel session."""

    if source_agent_id is not None and actor_user_id is not None:
        stmt = (
            select(LocalAgentChannelSession, ChatSession)
            .outerjoin(ChatSession, ChatSession.id == LocalAgentChannelSession.chat_session_id)
            .where(
                LocalAgentChannelSession.tenant_id == tenant_id,
                LocalAgentChannelSession.owner_user_id == owner_user_id,
                LocalAgentChannelSession.source == "web",
                LocalAgentChannelSession.status == "active",
                LocalAgentChannelSession.source_agent_id == source_agent_id,
                ChatSession.user_id == actor_user_id,
            )
            .order_by(LocalAgentChannelSession.created_at.desc(), LocalAgentChannelSession.id.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        rows = result.all()
        if rows:
            existing, chat_session = rows[0]
            return _session_payload(existing, chat_session)
    else:
        stmt = (
            select(LocalAgentChannelSession)
            .where(
                LocalAgentChannelSession.tenant_id == tenant_id,
                LocalAgentChannelSession.owner_user_id == owner_user_id,
                LocalAgentChannelSession.source == "web",
                LocalAgentChannelSession.status == "active",
            )
            .order_by(LocalAgentChannelSession.created_at.desc(), LocalAgentChannelSession.id.desc())
            .limit(1)
        )
        if source_agent_id is None:
            stmt = stmt.where(LocalAgentChannelSession.source_agent_id.is_(None))
        else:
            stmt = stmt.where(LocalAgentChannelSession.source_agent_id == source_agent_id)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is not None:
            return _session_payload(existing)

    chat_session: ChatSession | None = None
    if source_agent_id is not None:
        actor_id = actor_user_id or owner_user_id
        external_conversation_id = _local_agent_external_conversation_id(
            owner_user_id=owner_user_id,
            actor_user_id=actor_id,
            source_agent_id=source_agent_id,
            source="web",
        )
        chat_session = await create_or_bind_chat_session(
            db=db,
            tenant_id=tenant_id,
            agent_id=source_agent_id,
            user_id=actor_id,
            runtime_source="local_agent_channel",
            actor_type="local_agent",
            external_conversation_id=external_conversation_id,
            source_channel="local_agent",
            title_seed=title or "Local Agent",
            session_kind="local_agent_channel",
            visibility_scope="direct_user",
            listed_surface="chat",
        )

    channel_session = LocalAgentChannelSession(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        chat_session_id=chat_session.id if chat_session else None,
        source="web",
        status="active",
    )
    db.add(channel_session)
    await db.flush()
    await db.commit()
    return _session_payload(channel_session)


async def create_browser_session_ws_ticket(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
    session_id: uuid.UUID,
    ttl_seconds: int = DEFAULT_BROWSER_WS_TICKET_SECONDS,
) -> dict[str, Any]:
    """Create a short-lived browser ticket for subscribing to one local-agent session."""

    _session, owner_user_id = await get_channel_session_for_actor(
        db, session_id=session_id, actor_user_id=actor_user_id
    )
    expires_at = utcnow() + timedelta(seconds=ttl_seconds)
    token = jwt.encode(
        {
            "sub": str(owner_user_id),
            "actor": str(actor_user_id),
            "tid": str(tenant_id) if tenant_id else None,
            "sid": str(session_id),
            "scope": "local_agent:browser_ws",
            "exp": expires_at,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return {"ticket": f"{BROWSER_WS_TICKET_PREFIX}{token}", "expires_in": ttl_seconds, "single_use": False}


async def resolve_browser_session_ws_ticket(
    db: AsyncSession,
    *,
    ticket: str,
    session_id: uuid.UUID,
) -> dict[str, uuid.UUID | None]:
    """Validate a browser session ticket and return the user/session binding."""

    raw_token = _parse_browser_ws_ticket(ticket)
    try:
        payload = jwt.decode(raw_token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid local agent browser ticket")
    if payload.get("scope") != "local_agent:browser_ws" or payload.get("sid") != str(session_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local agent browser ticket scope mismatch")
    owner_user_id = uuid.UUID(str(payload["sub"]))
    tenant_id = uuid.UUID(str(payload["tid"])) if payload.get("tid") else None
    if tenant_id is not None:
        await pin_rls_tenant_context(db, tenant_id)
    await get_channel_session(db, session_id=session_id, owner_user_id=owner_user_id)
    return {"tenant_id": tenant_id, "owner_user_id": owner_user_id, "session_id": session_id}


async def _get_active_channel(db: AsyncSession, *, context: BridgeAuthContext) -> LocalAgentChannel | None:
    result = await db.execute(
        select(LocalAgentChannel).where(
            LocalAgentChannel.connection_id == context.connection_id,
            LocalAgentChannel.tenant_id == context.tenant_id,
            LocalAgentChannel.owner_user_id == context.user_id,
        )
    )
    return result.scalar_one_or_none()


async def _agent_capability_names(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    server_capabilities: tuple[str, ...],
) -> tuple[str, ...]:
    if context.agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Local agent bridge connection is not bound to an Agent",
        )
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.id == context.agent_id,
                Agent.tenant_id == context.tenant_id,
                Agent.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    owner_id = (
        getattr(agent, "owner_user_id", None)
        or getattr(agent, "creator_id", None)
        if agent is not None
        else None
    )
    if agent is None or owner_id != context.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local Agent ownership mismatch")

    policies = (
        (
            await db.execute(
                select(CapabilityPolicy).where(
                    CapabilityPolicy.tenant_id == context.tenant_id,
                    or_(CapabilityPolicy.agent_id.is_(None), CapabilityPolicy.agent_id == context.agent_id),
                    CapabilityPolicy.capability.in_(
                        tuple(f"local_agent.{capability}" for capability in server_capabilities)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    tenant_policies = {row.capability: row for row in policies if row.agent_id is None}
    agent_policies = {row.capability: row for row in policies if row.agent_id == context.agent_id}
    allowed: list[str] = []
    for capability in server_capabilities:
        policy_name = f"local_agent.{capability}"
        policy = agent_policies.get(policy_name) or tenant_policies.get(policy_name)
        # The snapshot advertises supported governed capabilities. A live
        # per-action check below decides whether an invocation is immediate or
        # waits for an owner decision. Missing policy is always deny.
        if policy is None or not policy.allowed:
            continue
        allowed.append(capability)
    return tuple(sorted(allowed))


async def _effective_capability_policy(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    capability: str,
) -> CapabilityPolicy:
    policies = (
        (
            await db.execute(
                select(CapabilityPolicy).where(
                    CapabilityPolicy.tenant_id == tenant_id,
                    or_(CapabilityPolicy.agent_id.is_(None), CapabilityPolicy.agent_id == agent_id),
                    CapabilityPolicy.capability == capability,
                )
            )
        )
        .scalars()
        .all()
    )
    agent_policy = next((row for row in policies if row.agent_id == agent_id), None)
    tenant_policy = next((row for row in policies if row.agent_id is None), None)
    policy = agent_policy or tenant_policy
    if policy is None or not policy.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Local Agent capability denied by live policy: {capability}",
        )
    return policy


def _enforce_action_conditions(
    policy: CapabilityPolicy,
    *,
    source: str,
    content: str,
    attachments: list[dict[str, Any]],
) -> None:
    conditions = dict(policy.conditions or {})
    allowed_sources = conditions.get("allowed_sources")
    if isinstance(allowed_sources, list) and source not in {str(value) for value in allowed_sources}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local Agent source denied by policy")
    max_content_chars = conditions.get("max_content_chars")
    if isinstance(max_content_chars, int) and max_content_chars >= 0 and len(content) > max_content_chars:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Local Agent content exceeds policy"
        )
    max_attachments = conditions.get("max_attachments")
    if isinstance(max_attachments, int) and max_attachments >= 0 and len(attachments) > max_attachments:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Local Agent attachments exceed policy",
        )


async def _active_snapshot_for_context(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    capability: str,
) -> tuple[LocalAgentChannel, LocalAgentCapabilitySnapshot]:
    channel = await _get_active_channel(db, context=context)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Local Agent channel is not ready")
    stmt = select(LocalAgentCapabilitySnapshot).where(
        LocalAgentCapabilitySnapshot.channel_id == channel.id,
        LocalAgentCapabilitySnapshot.tenant_id == context.tenant_id,
        LocalAgentCapabilitySnapshot.revoked_at.is_(None),
        LocalAgentCapabilitySnapshot.expires_at > utcnow(),
    )
    if context.agent_id is not None:
        stmt = stmt.where(LocalAgentCapabilitySnapshot.subject_agent_id == context.agent_id)
    snapshot = (
        await db.execute(
            stmt.order_by(
                LocalAgentCapabilitySnapshot.version.desc(),
                LocalAgentCapabilitySnapshot.issued_at.desc(),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if snapshot is None or not verify_capability_snapshot(
        capability_snapshot_payload(snapshot),
        signing_secret=local_capability_signing_secret(),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Local Agent capability snapshot is invalid or expired"
        )
    if capability not in set(snapshot.effective_capabilities_json or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Local Agent capability denied: {capability}",
        )
    return channel, snapshot


async def _active_snapshot_for_session(
    db: AsyncSession,
    *,
    session: LocalAgentChannelSession,
    capability: str,
) -> tuple[LocalAgentChannel, LocalAgentCapabilitySnapshot]:
    stmt = (
        select(LocalAgentChannel, LocalAgentCapabilitySnapshot)
        .join(LocalAgentCapabilitySnapshot, LocalAgentCapabilitySnapshot.channel_id == LocalAgentChannel.id)
        .where(
            LocalAgentChannel.tenant_id == session.tenant_id,
            LocalAgentChannel.owner_user_id == session.owner_user_id,
            LocalAgentCapabilitySnapshot.revoked_at.is_(None),
            LocalAgentCapabilitySnapshot.expires_at > utcnow(),
        )
    )
    if session.source_agent_id is not None:
        stmt = stmt.where(LocalAgentCapabilitySnapshot.subject_agent_id == session.source_agent_id)
    rows = (
        await db.execute(
            stmt.order_by(
                LocalAgentCapabilitySnapshot.issued_at.desc(),
                LocalAgentCapabilitySnapshot.version.desc(),
            ).limit(1)
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No valid Local Agent capability snapshot")
    channel, snapshot = rows[0]
    if not verify_capability_snapshot(
        capability_snapshot_payload(snapshot),
        signing_secret=local_capability_signing_secret(),
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local Agent capability snapshot is invalid")
    if capability not in set(snapshot.effective_capabilities_json or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Local Agent capability denied: {capability}",
        )
    return channel, snapshot


async def _allocate_event_sequence(db: AsyncSession, *, session_id: uuid.UUID) -> int:
    if not isinstance(db, AsyncSession):
        return uuid.uuid4().int & ((1 << 63) - 1)
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "") == "postgresql":
        lock_key = int.from_bytes(session_id.bytes[:8], byteorder="big", signed=True)
        await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
    result = await db.execute(
        select(func.coalesce(func.max(LocalAgentChannelEvent.sequence), 0) + 1).where(
            LocalAgentChannelEvent.session_id == session_id
        )
    )
    return int(result.scalar_one())


def _result_refs(artifacts: list[dict[str, Any]] | None) -> list[str]:
    refs: list[str] = []
    for artifact in artifacts or []:
        if not isinstance(artifact, dict):
            continue
        for key in ("source_ref", "uri", "path", "id"):
            value = str(artifact.get(key) or "").strip()
            if value:
                refs.append(value)
                break
    return list(dict.fromkeys(refs))


def _add_remote_action_span(
    db: AsyncSession,
    *,
    message: LocalAgentChannelMessage,
    session: LocalAgentChannelSession,
) -> None:
    if not isinstance(db, AsyncSession):
        return
    receipt = _receipt_for_message(message)
    db.add(
        InvocationSpan(
            tenant_id=session.tenant_id,
            agent_id=session.source_agent_id,
            user_id=message.sender_user_id,
            execution_identity_type="agent" if session.source_agent_id else "user",
            execution_identity_id=session.source_agent_id or message.sender_user_id,
            session_id=str(session.id),
            request_id=message.id,
            decision_id=f"local-capability:{message.capability_snapshot_hash}",
            input_hash=message.request_hash,
            idempotency_key=message.idempotency_key,
            side_effect_refs=[],
            trace_id=str(message.receipt_trace_id),
            span_id=str(message.receipt_span_id),
            span_type="remote_action",
            name="local_agent.execute",
            status="waiting" if message.status == "waiting_approval" else "running",
            started_at=utcnow(),
            duration_ms=0.0,
            evidence_refs=[f"local-agent-message://{message.id}"],
            truth_evidence_json=[f"local-agent-message://{message.id}"],
            metadata_json={"execution_receipt": receipt or {}},
        )
    )


async def _complete_remote_action_span(
    db: AsyncSession,
    *,
    message: LocalAgentChannelMessage,
    receipt: dict[str, Any] | None,
    failed: bool,
) -> None:
    if not isinstance(db, AsyncSession) or receipt is None:
        return
    span = (
        await db.execute(
            select(InvocationSpan).where(
                InvocationSpan.tenant_id == message.tenant_id,
                InvocationSpan.trace_id == message.receipt_trace_id,
                InvocationSpan.span_id == message.receipt_span_id,
            )
        )
    ).scalar_one_or_none()
    if span is None:
        return
    ended_at = utcnow()
    span.status = "error" if failed else "ok"
    span.ended_at = ended_at
    span.duration_ms = max(0.0, (ended_at - span.started_at).total_seconds() * 1000.0)
    span.side_effect_refs = list(receipt.get("result_refs") or [])
    span.metadata_json = {**dict(span.metadata_json or {}), "execution_receipt": receipt}
    span.error = message.result if failed else None


async def mark_channel_ready(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    runtime_kind: str,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue an immutable signed capability snapshot and mark the channel online."""

    _require_scope(context, "local_agent:connect")
    server_capabilities = _server_capability_names(context)
    agent_capabilities = await _agent_capability_names(
        db,
        context=context,
        server_capabilities=server_capabilities,
    )
    reported_capabilities = _reported_capability_names(capabilities)
    effective_capabilities = effective_local_capabilities(
        server_capabilities=server_capabilities,
        agent_capabilities=agent_capabilities,
        reported_capabilities=reported_capabilities,
    )
    channel = await _get_active_channel(db, context=context)
    if channel is None:
        channel = LocalAgentChannel(
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
            connection_id=context.connection_id,
        )
        db.add(channel)
    channel.runtime_kind = (runtime_kind or context.client_kind or "local_agent")[:64]
    channel.status = "online"
    channel.last_seen_at = utcnow()
    await db.flush()
    previous_version = await db.scalar(
        select(func.coalesce(func.max(LocalAgentCapabilitySnapshot.version), 0)).where(
            LocalAgentCapabilitySnapshot.channel_id == channel.id
        )
    )
    issued_at = utcnow()
    ttl_seconds = max(
        60,
        int(getattr(settings, "LOCAL_AGENT_CAPABILITY_SNAPSHOT_TTL_SECONDS", DEFAULT_CAPABILITY_SNAPSHOT_SECONDS)),
    )
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    signed = build_signed_capability_snapshot(
        signing_secret=local_capability_signing_secret(),
        issuer="hive.local-agent-channel",
        subject_agent_id=_uuid(context.agent_id),
        tenant_id=context.tenant_id,
        scopes=effective_capabilities,
        issued_at=issued_at,
        expires_at=expires_at,
        version=int(previous_version or 0) + 1,
    )
    await db.execute(
        update(LocalAgentCapabilitySnapshot)
        .where(
            LocalAgentCapabilitySnapshot.channel_id == channel.id,
            LocalAgentCapabilitySnapshot.revoked_at.is_(None),
        )
        .values(revoked_at=issued_at)
    )
    snapshot = LocalAgentCapabilitySnapshot(
        tenant_id=context.tenant_id,
        channel_id=channel.id,
        connection_id=context.connection_id,
        subject_agent_id=_uuid(context.agent_id),
        issuer=str(signed["issuer"]),
        version=int(signed["version"]),
        reported_capabilities_json=list(reported_capabilities),
        server_capabilities_json=list(server_capabilities),
        agent_capabilities_json=list(agent_capabilities),
        effective_capabilities_json=list(effective_capabilities),
        snapshot_hash=str(signed["snapshot_hash"]),
        signature=str(signed["signature"]),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    db.add(snapshot)
    channel.capabilities_json = {
        "schema": "hive.local_channel_capabilities.v1",
        "reported": list(reported_capabilities),
        "effective": list(effective_capabilities),
        "snapshot_hash": snapshot.snapshot_hash,
        "snapshot_required": True,
    }
    await db.flush()
    await db.commit()
    return {
        "status": "online",
        "channel_id": str(channel.id),
        "runtime_kind": channel.runtime_kind,
        "last_seen_at": channel.last_seen_at.isoformat() if channel.last_seen_at else None,
        "snapshot_hash": snapshot.snapshot_hash,
        "signature": snapshot.signature,
        "snapshot_version": snapshot.version,
        "effective_capabilities": list(effective_capabilities),
        "expires_at": snapshot.expires_at.isoformat(),
    }


async def mark_channel_seen(db: AsyncSession, *, context: BridgeAuthContext) -> None:
    """Refresh runner presence while the channel WebSocket remains alive."""

    _require_scope(context, "local_agent:connect")
    channel = await _get_active_channel(db, context=context)
    if channel is None:
        return
    channel.status = "online"
    channel.last_seen_at = utcnow()
    await db.commit()


async def mark_channel_offline(db: AsyncSession, *, context: BridgeAuthContext) -> None:
    channel = await _get_active_channel(db, context=context)
    if channel is None:
        return
    channel.status = "stale"
    channel.last_seen_at = utcnow()
    await db.commit()


async def create_channel_session(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    source_agent_id: uuid.UUID | None = None,
    source: str = "web",
    title: str | None = None,
) -> dict[str, Any]:
    """Create a user-owned Local Agent Channel session.

    Direct web chat is user-scoped and may not have a cloud agent chat session.
    A2A calls can pass `source_agent_id`, which mirrors the transcript into the
    calling agent's ChatSession while keeping delivery bound to the owner user.
    """

    chat_session: ChatSession | None = None
    if source_agent_id is not None:
        actor_id = actor_user_id or owner_user_id
        external_conversation_id = _local_agent_external_conversation_id(
            owner_user_id=owner_user_id,
            actor_user_id=actor_id,
            source_agent_id=source_agent_id,
            source=source,
        )
        chat_session = await create_or_bind_chat_session(
            db=db,
            tenant_id=tenant_id,
            agent_id=source_agent_id,
            user_id=actor_id,
            runtime_source="local_agent_channel",
            actor_type="local_agent",
            external_conversation_id=external_conversation_id,
            source_channel="local_agent",
            title_seed=title or "Local Agent",
            session_kind="local_agent_channel",
            visibility_scope="direct_user",
            listed_surface="chat",
        )
    channel_session = LocalAgentChannelSession(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        chat_session_id=chat_session.id if chat_session else None,
        source=(source or "web")[:32],
        status="active",
    )
    db.add(channel_session)
    await db.flush()
    await db.commit()
    return {
        "id": channel_session.id,
        "chat_session_id": chat_session.id if chat_session else None,
        "source": channel_session.source,
        "status": channel_session.status,
        "created_at": channel_session.created_at,
    }


async def enqueue_channel_message(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    sender_user_id: uuid.UUID | None,
    sender_agent_id: uuid.UUID | None = None,
    content: str,
    attachments: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue one governed remote action with stable idempotency and receipt identity."""

    result = await db.execute(
        select(LocalAgentChannelSession).where(
            LocalAgentChannelSession.id == session_id,
            LocalAgentChannelSession.owner_user_id == owner_user_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel session not found")
    clean_attachments = list(attachments or [])
    clean_metadata = {"source": session.source, **dict(metadata or {})}
    clean_idempotency_key = str(idempotency_key or f"local:{uuid.uuid4()}").strip()[:200]
    request_hash = _canonical_hash(
        {
            "session_id": str(session.id),
            "owner_user_id": str(session.owner_user_id),
            "sender_user_id": str(sender_user_id) if sender_user_id else None,
            "sender_agent_id": str(sender_agent_id) if sender_agent_id else None,
            "content": str(content),
            "attachments": clean_attachments,
            "metadata": clean_metadata,
        }
    )
    existing = (
        await db.execute(
            select(LocalAgentChannelMessage).where(
                LocalAgentChannelMessage.tenant_id == session.tenant_id,
                LocalAgentChannelMessage.idempotency_key == clean_idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.owner_user_id != owner_user_id or existing.request_hash != request_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Local Agent idempotency key was reused with a different request",
            )
        return _message_payload(existing)

    snapshot_hash = "compatibility-test"
    channel = None
    snapshot = None
    execute_policy: CapabilityPolicy | None = None
    if isinstance(db, AsyncSession):
        channel, snapshot = await _active_snapshot_for_session(db, session=session, capability="execute")
        snapshot_hash = snapshot.snapshot_hash
        session.channel_id = channel.id
        session.connection_id = channel.connection_id
        if session.source_agent_id is None:
            session.source_agent_id = snapshot.subject_agent_id
        execute_policy = await _effective_capability_policy(
            db,
            tenant_id=session.tenant_id,
            agent_id=snapshot.subject_agent_id,
            capability="local_agent.execute",
        )
        _enforce_action_conditions(
            execute_policy,
            source=session.source,
            content=str(content),
            attachments=clean_attachments,
        )
    message_id = uuid.uuid4()
    replay_key = f"local:{hashlib.sha256(clean_idempotency_key.encode('utf-8')).hexdigest()}"
    trace_id = f"local-agent:{message_id}"
    span_id = f"remote-action:{message_id}"
    message = LocalAgentChannelMessage(
        id=message_id,
        tenant_id=session.tenant_id,
        owner_user_id=session.owner_user_id,
        source_agent_id=session.source_agent_id,
        session_id=session.id,
        sender_user_id=sender_user_id,
        sender_agent_id=sender_agent_id,
        direction="hive_to_local",
        content=content,
        attachments_json=clean_attachments,
        metadata_json=clean_metadata,
        idempotency_key=clean_idempotency_key,
        request_hash=request_hash,
        capability_snapshot_hash=snapshot_hash,
        replay_key=replay_key,
        receipt_trace_id=trace_id,
        receipt_span_id=span_id,
        status="waiting_approval" if execute_policy is not None and execute_policy.requires_approval else "pending",
    )
    db.add(message)
    await db.flush()
    if execute_policy is not None and execute_policy.requires_approval:
        agent = await db.get(Agent, snapshot.subject_agent_id)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local Agent no longer exists")
        from app.services.approval_service import approval_service

        approval_result = await approval_service.request_approval(
            db,
            agent,
            action_type="local_agent.execute",
            details={
                "requested_by": str(sender_user_id or owner_user_id),
                "reason": "Owner approval is required before dispatch to the trusted local runtime.",
                "origin": {
                    "type": "local_agent_channel",
                    "session_id": str(session.id),
                    "source": session.source,
                },
                "local_agent_message_id": str(message.id),
                "request_hash": request_hash,
                "replay_key": replay_key,
                "attachment_count": len(clean_attachments),
                "content_length": len(str(content)),
                "policy_snapshot": {
                    "capability": execute_policy.capability,
                    "tenant_id": str(session.tenant_id),
                    "agent_id": str(snapshot.subject_agent_id),
                    "requires_approval": True,
                    "conditions": dict(execute_policy.conditions or {}),
                    "capability_snapshot_hash": snapshot_hash,
                },
            },
        )
        message.approval_id = uuid.UUID(str(approval_result["approval_id"]))
    _add_remote_action_span(db, message=message, session=session)
    if session.chat_session_id and session.source_agent_id:
        db.add(
            ChatMessage(
                agent_id=session.source_agent_id,
                tenant_id=session.tenant_id,
                user_id=sender_user_id,
                role="user",
                content=content,
                conversation_id=str(session.chat_session_id),
            )
        )
    event = LocalAgentChannelEvent(
        sequence=await _allocate_event_sequence(db, session_id=session.id),
        tenant_id=session.tenant_id,
        owner_user_id=session.owner_user_id,
        source_agent_id=session.source_agent_id,
        session_id=session.id,
        message_id=message.id,
        direction="hive_to_local",
        event_type="approval_required" if message.status == "waiting_approval" else "message",
        payload_json=_message_payload(message),
    )
    db.add(event)
    await db.flush()
    await db.commit()
    return {**_message_payload(message), "event": _event_payload(event)}


async def list_channel_events(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID | None = None,
    after_sequence: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = select(LocalAgentChannelEvent).where(LocalAgentChannelEvent.session_id == session_id)
    if owner_user_id is not None:
        stmt = stmt.where(LocalAgentChannelEvent.owner_user_id == owner_user_id)
    stmt = stmt.where(LocalAgentChannelEvent.sequence > max(0, int(after_sequence)))
    stmt = stmt.order_by(LocalAgentChannelEvent.sequence.asc()).limit(limit)
    result = await db.execute(stmt)
    return [_event_payload(event) for event in result.scalars().all()]


async def apply_local_agent_action_approval(
    db: AsyncSession,
    *,
    approval: ApprovalRequest,
) -> dict[str, Any] | None:
    """Apply a standard approval decision to exactly one durable local action."""

    if approval.action_type != "local_agent.execute":
        return None
    raw_message_id = str((approval.details or {}).get("local_agent_message_id") or "").strip()
    try:
        message_id = uuid.UUID(raw_message_id)
    except ValueError as exc:
        raise ValueError("Local Agent approval is missing a valid message binding") from exc
    message = (
        await db.execute(
            select(LocalAgentChannelMessage)
            .where(
                LocalAgentChannelMessage.id == message_id,
                LocalAgentChannelMessage.approval_id == approval.id,
                LocalAgentChannelMessage.tenant_id == approval.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if message is None:
        raise ValueError("Local Agent approval message binding not found")
    if message.status != "waiting_approval":
        if message.status in {"pending", "delivered", "rejected"}:
            return _message_payload(message)
        raise ValueError(f"Local Agent message cannot resolve approval from {message.status}")

    approved = approval.status == "approved"
    policy_changed = False
    if approved:
        try:
            policy = await _effective_capability_policy(
                db,
                tenant_id=message.tenant_id,
                agent_id=message.source_agent_id,
                capability="local_agent.execute",
            )
            _enforce_action_conditions(
                policy,
                source=str((message.metadata_json or {}).get("source") or "unknown"),
                content=message.content,
                attachments=list(message.attachments_json or []),
            )
        except HTTPException:
            approved = False
            policy_changed = True

    message.status = "pending" if approved else "rejected"
    message.completed_at = None if approved else utcnow()
    if approved:
        approval.execution_status = "approved"
    elif policy_changed:
        message.result = "Local Agent policy changed before approval release."
        approval.execution_status = "failed"
        approval.execution_result = message.result
    else:
        message.result = "Owner rejected this Local Agent action."
        approval.execution_status = "rejected"
    if not approved:
        await _complete_remote_action_span(
            db,
            message=message,
            receipt=_receipt_for_message(message, receipt_status=message.status),
            failed=True,
        )
    event = LocalAgentChannelEvent(
        sequence=await _allocate_event_sequence(db, session_id=message.session_id),
        tenant_id=message.tenant_id,
        owner_user_id=message.owner_user_id,
        source_agent_id=message.source_agent_id,
        session_id=message.session_id,
        message_id=message.id,
        direction="hive_to_local",
        event_type="approval_resolved",
        payload_json={
            "approval_id": str(approval.id),
            "status": approval.status,
            "execution_status": approval.execution_status,
            "message_status": message.status,
            "request_hash": message.request_hash,
            "reason": message.result,
        },
    )
    db.add(event)
    await db.flush()
    return {**_message_payload(message), "event": _event_payload(event)}


async def notify_local_agent_action_resolution(payload: dict[str, Any] | None) -> None:
    """Best-effort live wakeup; durable ping/poll remains the recovery path."""

    if not payload or payload.get("status") != "pending":
        return
    try:
        from app.api.local_agent_channel import channel_ws_manager

        await channel_ws_manager.send_to_user(
            uuid.UUID(str(payload["owner_user_id"])),
            {"type": "message", "message": payload},
        )
    except Exception:
        # The transaction is already durable. An online runner polls on ping,
        # while an offline runner claims the same replay_key on reconnect.
        return


async def _reconcile_stale_deliveries(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
) -> int:
    now = utcnow()
    stale = (
        (
            await db.execute(
                select(LocalAgentChannelMessage)
                .where(
                    LocalAgentChannelMessage.owner_user_id == context.user_id,
                    LocalAgentChannelMessage.tenant_id == context.tenant_id,
                    LocalAgentChannelMessage.direction == "hive_to_local",
                    LocalAgentChannelMessage.status == "delivered",
                    LocalAgentChannelMessage.delivery_lease_expires_at.is_not(None),
                    LocalAgentChannelMessage.delivery_lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for message in stale:
        if int(message.delivery_attempt_count or 0) >= MAX_DELIVERY_ATTEMPTS:
            message.status = "needs_reconciliation"
            message.delivery_lease_expires_at = None
            event_type = "delivery_reconciliation_required"
        else:
            message.status = "pending"
            message.delivery_lease_expires_at = None
            event_type = "delivery_requeued"
        db.add(
            LocalAgentChannelEvent(
                sequence=await _allocate_event_sequence(db, session_id=message.session_id),
                tenant_id=message.tenant_id,
                owner_user_id=message.owner_user_id,
                source_agent_id=message.source_agent_id,
                session_id=message.session_id,
                message_id=message.id,
                direction="hive_to_local",
                event_type=event_type,
                payload_json={
                    "replay_key": message.replay_key,
                    "delivery_attempt_count": int(message.delivery_attempt_count or 0),
                },
            )
        )
    if stale:
        await db.flush()
    return len(stale)


async def poll_pending_channel_messages(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Lease pending work; stale deliveries requeue with the same replay key."""

    _require_scope(context, "local_agent:receive")
    snapshot = None
    if isinstance(db, AsyncSession):
        _channel, snapshot = await _active_snapshot_for_context(db, context=context, capability="execute")
        await _reconcile_stale_deliveries(db, context=context)
    result = await db.execute(
        select(LocalAgentChannelMessage)
        .join(LocalAgentChannelSession, LocalAgentChannelSession.id == LocalAgentChannelMessage.session_id)
        .where(
            LocalAgentChannelMessage.owner_user_id == context.user_id,
            LocalAgentChannelMessage.tenant_id == context.tenant_id,
            LocalAgentChannelMessage.direction == "hive_to_local",
            LocalAgentChannelMessage.status == "pending",
        )
        .order_by(LocalAgentChannelMessage.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    candidates = result.scalars().all()
    messages: list[LocalAgentChannelMessage] = []
    snapshot_hash = snapshot.snapshot_hash if snapshot is not None else "compatibility-test"
    for message in candidates:
        if context.agent_id is not None and message.source_agent_id not in {None, context.agent_id}:
            continue
        messages.append(message)
    now = utcnow()
    lease_seconds = max(
        10,
        int(getattr(settings, "LOCAL_AGENT_DELIVERY_LEASE_SECONDS", DEFAULT_DELIVERY_LEASE_SECONDS)),
    )
    for message in messages:
        message.status = "delivered"
        message.delivered_at = now
        message.delivery_attempt_count = int(message.delivery_attempt_count or 0) + 1
        message.delivery_lease_expires_at = now + timedelta(seconds=lease_seconds)
        message.metadata_json = {
            **dict(message.metadata_json or {}),
            "last_delivery_snapshot_hash": snapshot_hash,
        }
    if messages or isinstance(db, AsyncSession):
        await db.commit()
    return [_message_payload(message) for message in messages]


async def ack_channel_message(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    message_id: uuid.UUID,
) -> dict[str, Any]:
    _require_scope(context, "local_agent:receive")
    if isinstance(db, AsyncSession):
        await _active_snapshot_for_context(db, context=context, capability="execute")
    result = await db.execute(
        select(LocalAgentChannelMessage).where(
            LocalAgentChannelMessage.id == message_id,
            LocalAgentChannelMessage.owner_user_id == context.user_id,
            LocalAgentChannelMessage.tenant_id == context.tenant_id,
        )
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel message not found")
    if message.status == "pending":
        message.status = "delivered"
        message.delivered_at = utcnow()
    if message.status == "delivered":
        activity_seconds = max(
            30,
            int(
                getattr(
                    settings,
                    "LOCAL_AGENT_DELIVERY_ACTIVITY_LEASE_SECONDS",
                    DEFAULT_DELIVERY_ACTIVITY_LEASE_SECONDS,
                )
            ),
        )
        message.delivery_lease_expires_at = utcnow() + timedelta(seconds=activity_seconds)
    await db.commit()
    return {"status": message.status}


async def record_channel_event(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    session_id: uuid.UUID,
    message_id: uuid.UUID | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_scope(context, "local_agent:send")
    if isinstance(db, AsyncSession):
        await _active_snapshot_for_context(db, context=context, capability="event_stream")
    session_result = await db.execute(
        select(LocalAgentChannelSession).where(
            LocalAgentChannelSession.id == session_id,
            LocalAgentChannelSession.owner_user_id == context.user_id,
            LocalAgentChannelSession.tenant_id == context.tenant_id,
        )
    )
    session = session_result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel session not found")
    if message_id is not None and isinstance(db, AsyncSession):
        bound_message = (
            await db.execute(
                select(LocalAgentChannelMessage).where(
                    LocalAgentChannelMessage.id == message_id,
                    LocalAgentChannelMessage.session_id == session.id,
                    LocalAgentChannelMessage.owner_user_id == context.user_id,
                )
            )
        ).scalar_one_or_none()
        if bound_message is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel message not found")
        if bound_message.status == "delivered":
            activity_seconds = max(
                30,
                int(
                    getattr(
                        settings,
                        "LOCAL_AGENT_DELIVERY_ACTIVITY_LEASE_SECONDS",
                        DEFAULT_DELIVERY_ACTIVITY_LEASE_SECONDS,
                    )
                ),
            )
            bound_message.delivery_lease_expires_at = utcnow() + timedelta(seconds=activity_seconds)
    chat_session: ChatSession | None = None
    chat_user_id = context.user_id
    if session.chat_session_id and hasattr(db, "get"):
        chat_session = await db.get(ChatSession, session.chat_session_id)
        if chat_session is not None and getattr(chat_session, "user_id", None):
            chat_user_id = chat_session.user_id
    event = LocalAgentChannelEvent(
        sequence=await _allocate_event_sequence(db, session_id=session.id),
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
        source_agent_id=session.source_agent_id,
        session_id=session.id,
        message_id=message_id,
        direction="local_to_hive",
        event_type=event_type[:64],
        payload_json=dict(payload or {}),
    )
    db.add(event)
    if event_type == "text":
        content = str((payload or {}).get("text") or (payload or {}).get("content") or "")
        if content and session.chat_session_id and session.source_agent_id:
            db.add(
                ChatMessage(
                    agent_id=session.source_agent_id,
                    tenant_id=context.tenant_id,
                    user_id=chat_user_id,
                    role="assistant",
                    content=content,
                    conversation_id=str(session.chat_session_id),
                )
            )
    await db.flush()
    await db.commit()
    return _event_payload(event)


async def record_channel_result(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    result_status: str,
    output: str,
    artifacts: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_scope(context, "local_agent:report")
    session_result = await db.execute(
        select(LocalAgentChannelSession).where(
            LocalAgentChannelSession.id == session_id,
            LocalAgentChannelSession.owner_user_id == context.user_id,
            LocalAgentChannelSession.tenant_id == context.tenant_id,
        )
    )
    session = session_result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel session not found")
    chat_session: ChatSession | None = None
    chat_user_id = context.user_id
    if session.chat_session_id and hasattr(db, "get"):
        chat_session = await db.get(ChatSession, session.chat_session_id)
        if chat_session is not None and getattr(chat_session, "user_id", None):
            chat_user_id = chat_session.user_id

    message_result = await db.execute(
        select(LocalAgentChannelMessage).where(
            LocalAgentChannelMessage.id == message_id,
            LocalAgentChannelMessage.session_id == session.id,
            LocalAgentChannelMessage.owner_user_id == context.user_id,
            LocalAgentChannelMessage.tenant_id == context.tenant_id,
        )
    )
    message = message_result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel message not found")
    if message.status in {"completed", "failed"}:
        return {
            "status": message.status,
            "event": None,
            "message": _message_payload(message),
            "receipt": _receipt_for_message(message),
            "idempotent_replay": True,
        }
    if isinstance(db, AsyncSession):
        await _active_snapshot_for_context(db, context=context, capability="result_report")

    normalized_status = "failed" if result_status == "failed" else "completed"
    message.status = normalized_status
    message.result = output
    result_refs = _result_refs(artifacts)
    receipt = _receipt_for_message(
        message,
        result_refs=result_refs,
        receipt_status=normalized_status,
    )
    message.metadata_json = {
        **dict(message.metadata_json or {}),
        "report": {**dict(metadata or {}), "artifacts": list(artifacts or [])},
        **(
            {"execution_receipt": receipt}
            if receipt is not None
            else {"receipt_unavailable_reason": "legacy_message_without_capability_snapshot"}
        ),
    }
    message.completed_at = utcnow()
    message.delivery_lease_expires_at = None
    event = LocalAgentChannelEvent(
        sequence=await _allocate_event_sequence(db, session_id=session.id),
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
        source_agent_id=session.source_agent_id,
        session_id=session.id,
        message_id=message.id,
        direction="local_to_hive",
        event_type="result",
        payload_json={
            "status": normalized_status,
            "output": output,
            "artifacts": list(artifacts or []),
            "metadata": dict(metadata or {}),
        },
    )
    db.add(event)
    if session.chat_session_id and session.source_agent_id:
        db.add(
            ChatMessage(
                agent_id=session.source_agent_id,
                tenant_id=context.tenant_id,
                user_id=chat_user_id,
                role="assistant",
                content=output,
                conversation_id=str(session.chat_session_id),
            )
        )
        if chat_session is not None:
            chat_session.last_message_at = utcnow()
    await _complete_remote_action_span(
        db,
        message=message,
        receipt=receipt,
        failed=normalized_status == "failed",
    )
    await db.flush()
    await db.commit()
    return {
        "status": normalized_status,
        "event": _event_payload(event),
        "message": _message_payload(message),
        "receipt": receipt,
        "idempotent_replay": False,
    }
