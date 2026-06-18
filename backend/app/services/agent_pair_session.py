"""Shared agent-pair session and participant ledger helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.gateway_message import GatewayMessage
from app.models.participant import Participant
from app.session_identifiers import (
    build_agent_pair_session_id,
    build_legacy_gateway_conversation_ids,
    canonicalize_agent_pair_ids,
)


def session_conversation_id(session: ChatSession) -> str:
    """Return the canonical conversation id used by transcript ledgers."""
    return str(session.id)


async def get_or_create_agent_participant_id(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | str,
    display_name: str,
    avatar_url: str | None = None,
) -> uuid.UUID | None:
    """Resolve the stable Participant id for an agent, creating it if missing."""
    agent_uuid = uuid.UUID(str(agent_id))
    result = await db.execute(
        select(Participant.id).where(
            Participant.type == "agent",
            Participant.ref_id == agent_uuid,
        )
    )
    participant_id = result.scalar_one_or_none()
    if participant_id:
        return participant_id

    participant = Participant(
        type="agent",
        ref_id=agent_uuid,
        display_name=(display_name or "Agent")[:100],
        avatar_url=avatar_url,
    )
    db.add(participant)
    flush = getattr(db, "flush", None)
    if flush:
        await flush()
    return participant.id


async def find_or_create_agent_pair_session(
    db: AsyncSession,
    *,
    source_agent_id: uuid.UUID | str,
    target_agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None = None,
    owner_user_id: uuid.UUID | str,
    source_agent_name: str,
    target_agent_name: str,
    source_participant_id: uuid.UUID | None = None,
) -> ChatSession:
    """Find or create the durable ChatSession for an agent pair."""
    source_uuid = uuid.UUID(str(source_agent_id))
    target_uuid = uuid.UUID(str(target_agent_id))
    tenant_uuid = uuid.UUID(str(tenant_id)) if tenant_id else None
    owner_uuid = uuid.UUID(str(owner_user_id))
    session_uuid = build_agent_pair_session_id(source_uuid, target_uuid)
    session_agent_id, peer_agent_id = canonicalize_agent_pair_ids(source_uuid, target_uuid)

    result = await db.execute(select(ChatSession).where(ChatSession.id == session_uuid))
    session = result.scalar_one_or_none()
    if session is None:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.source_channel == "agent",
                ChatSession.agent_id == session_agent_id,
                ChatSession.peer_agent_id == peer_agent_id,
            )
        )
        session = result.scalar_one_or_none()
    if session is not None and tenant_uuid and session.tenant_id is None:
        session.tenant_id = tenant_uuid

    if session is None:
        session = ChatSession(
            id=session_uuid,
            agent_id=session_agent_id,
            tenant_id=tenant_uuid,
            user_id=owner_uuid,
            title=f"{source_agent_name} ↔ {target_agent_name}",
            source_channel="agent",
            participant_id=source_participant_id,
            peer_agent_id=peer_agent_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(session)
        flush = getattr(db, "flush", None)
        if flush:
            await flush()

    conv_id = session_conversation_id(session)
    legacy_conv_ids = build_legacy_gateway_conversation_ids(source_uuid, target_uuid)
    chat_values = {"conversation_id": conv_id, "agent_id": session.agent_id}
    gateway_values = {"conversation_id": conv_id}
    if tenant_uuid:
        chat_values["tenant_id"] = tenant_uuid
        gateway_values["tenant_id"] = tenant_uuid

    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.conversation_id.in_(legacy_conv_ids))
        .values(**chat_values)
    )
    await db.execute(
        update(GatewayMessage)
        .where(GatewayMessage.conversation_id.in_(legacy_conv_ids))
        .values(**gateway_values)
    )
    return session
