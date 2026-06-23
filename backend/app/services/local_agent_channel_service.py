"""Local Agent Channel service.

This layer turns a bound local bridge connection into an IM-like channel:
browser/A2A messages are durably queued, the local runner consumes them over
WebSocket or fallback poll, and runner events are replayable through Hive
ChatSession plus local channel event rows.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.local_agent_channel import (
    LocalAgentChannel,
    LocalAgentChannelEvent,
    LocalAgentChannelMessage,
    LocalAgentChannelSession,
    LocalAgentChannelWsTicket,
)
from app.services.chat_artifact_delivery import create_or_bind_chat_session
from app.services.local_bridge_service import BridgeAuthContext, hash_secret, utcnow

WS_TICKET_PREFIX = "hbt_"
DEFAULT_WS_TICKET_SECONDS = 60


def _generate_ws_ticket() -> str:
    return f"{WS_TICKET_PREFIX}{secrets.token_urlsafe(32)}"


def _parse_ws_ticket(ticket: str) -> str:
    normalized = (ticket or "").strip()
    if not normalized.startswith(WS_TICKET_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid local agent channel ticket")
    return normalized


def _require_scope(context: BridgeAuthContext, *accepted_scopes: str) -> None:
    scopes = set(context.scopes or ())
    if not scopes.intersection(accepted_scopes):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bridge token lacks local agent channel scope")


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _event_payload(event: LocalAgentChannelEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "session_id": str(event.session_id),
        "message_id": str(event.message_id) if event.message_id else None,
        "direction": event.direction,
        "type": event.event_type,
        "payload": dict(event.payload_json or {}),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _message_payload(message: LocalAgentChannelMessage) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "session_id": str(message.session_id),
        "owner_user_id": str(message.owner_user_id),
        "source_agent_id": str(message.source_agent_id) if message.source_agent_id else None,
        "tenant_id": str(message.tenant_id) if message.tenant_id else None,
        "direction": message.direction,
        "content": message.content,
        "attachments": list(message.attachments_json or []),
        "metadata": dict(message.metadata_json or {}),
        "status": message.status,
        "result": message.result,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
        "completed_at": message.completed_at.isoformat() if message.completed_at else None,
    }


async def create_ws_ticket(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    ttl_seconds: int = DEFAULT_WS_TICKET_SECONDS,
) -> dict[str, Any]:
    """Create a short-lived single-use ticket for the local runner WebSocket."""

    _require_scope(context, "local_agent:connect", "gateway:poll")
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
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid local agent channel ticket")
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
        agent_id=None,
        user_id=row.user_id,
        scopes=tuple(row.scopes or []),
        client_kind=str((row.metadata_json or {}).get("client_kind") or "local_agent"),
        device_name=str((row.metadata_json or {}).get("device_name") or "Local Agent"),
    )


async def _get_active_channel(db: AsyncSession, *, context: BridgeAuthContext) -> LocalAgentChannel | None:
    result = await db.execute(
        select(LocalAgentChannel).where(
            LocalAgentChannel.connection_id == context.connection_id,
            LocalAgentChannel.tenant_id == context.tenant_id,
            LocalAgentChannel.owner_user_id == context.user_id,
        )
    )
    return result.scalar_one_or_none()


async def mark_channel_ready(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    runtime_kind: str,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a local bridge connection as online for the Local Agent Channel."""

    _require_scope(context, "local_agent:connect", "gateway:poll")
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
    channel.capabilities_json = dict(capabilities or {})
    channel.last_seen_at = utcnow()
    await db.flush()
    await db.commit()
    return {
        "status": "online",
        "channel_id": str(channel.id),
        "runtime_kind": channel.runtime_kind,
        "last_seen_at": channel.last_seen_at.isoformat() if channel.last_seen_at else None,
    }


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
        external_conversation_id = f"local_agent:{owner_user_id}:{source_agent_id}:{source}"
        chat_session = await create_or_bind_chat_session(
            db=db,
            tenant_id=tenant_id,
            agent_id=source_agent_id,
            user_id=owner_user_id,
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
) -> dict[str, Any]:
    """Queue a Hive -> local message and mirror it into ChatSession when present."""

    result = await db.execute(
        select(LocalAgentChannelSession).where(
            LocalAgentChannelSession.id == session_id,
            LocalAgentChannelSession.owner_user_id == owner_user_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local agent channel session not found")
    message = LocalAgentChannelMessage(
        tenant_id=session.tenant_id,
        owner_user_id=session.owner_user_id,
        source_agent_id=session.source_agent_id,
        session_id=session.id,
        sender_user_id=sender_user_id,
        sender_agent_id=sender_agent_id,
        direction="hive_to_local",
        content=content,
        attachments_json=list(attachments or []),
        metadata_json={"source": session.source, **dict(metadata or {})},
        status="pending",
    )
    db.add(message)
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
    await db.flush()
    db.add(
        LocalAgentChannelEvent(
            tenant_id=session.tenant_id,
            owner_user_id=session.owner_user_id,
            source_agent_id=session.source_agent_id,
            session_id=session.id,
            message_id=message.id,
            direction="hive_to_local",
            event_type="message",
            payload_json=_message_payload(message),
        )
    )
    await db.commit()
    return _message_payload(message)


async def list_channel_events(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID | None = None,
    after_event_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = select(LocalAgentChannelEvent).where(LocalAgentChannelEvent.session_id == session_id)
    if owner_user_id is not None:
        stmt = stmt.where(LocalAgentChannelEvent.owner_user_id == owner_user_id)
    stmt = stmt.order_by(LocalAgentChannelEvent.created_at.asc(), LocalAgentChannelEvent.id.asc()).limit(limit)
    if after_event_id:
        # UUID ordering is not a stable cursor. For P0 this only filters already
        # seen rows by id; clients should prefer full refresh after reconnect.
        stmt = stmt.where(LocalAgentChannelEvent.id != after_event_id)
    result = await db.execute(stmt)
    return [_event_payload(event) for event in result.scalars().all()]


async def poll_pending_channel_messages(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fetch and mark pending Hive -> local channel messages."""

    _require_scope(context, "local_agent:receive", "gateway:poll")
    result = await db.execute(
        select(LocalAgentChannelMessage)
        .where(
            LocalAgentChannelMessage.owner_user_id == context.user_id,
            LocalAgentChannelMessage.tenant_id == context.tenant_id,
            LocalAgentChannelMessage.direction == "hive_to_local",
            LocalAgentChannelMessage.status == "pending",
        )
        .order_by(LocalAgentChannelMessage.created_at.asc())
        .limit(limit)
    )
    messages = result.scalars().all()
    now = utcnow()
    for message in messages:
        message.status = "delivered"
        message.delivered_at = now
    if messages:
        await db.commit()
    return [_message_payload(message) for message in messages]


async def ack_channel_message(
    db: AsyncSession,
    *,
    context: BridgeAuthContext,
    message_id: uuid.UUID,
) -> dict[str, Any]:
    _require_scope(context, "local_agent:receive", "gateway:poll")
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
    _require_scope(context, "local_agent:send", "gateway:report")
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
    event = LocalAgentChannelEvent(
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
                    user_id=context.user_id,
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
    _require_scope(context, "local_agent:report", "gateway:report")
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

    normalized_status = "failed" if result_status == "failed" else "completed"
    message.status = normalized_status
    message.result = output
    message.attachments_json = list(artifacts or [])
    message.metadata_json = {
        **dict(message.metadata_json or {}),
        "report": dict(metadata or {}),
    }
    message.completed_at = utcnow()
    event = LocalAgentChannelEvent(
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
                user_id=context.user_id,
                role="assistant",
                content=output,
                conversation_id=str(session.chat_session_id),
            )
        )
        chat_session = await db.get(ChatSession, session.chat_session_id) if hasattr(db, "get") else None
        if chat_session is not None:
            chat_session.last_message_at = utcnow()
    await db.flush()
    await db.commit()
    return {"status": normalized_status, "event": _event_payload(event), "message": _message_payload(message)}
