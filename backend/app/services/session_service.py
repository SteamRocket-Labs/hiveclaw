"""Unified session factory/service for core chat session flows."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.session_identifiers import (
    build_legacy_gateway_conversation_ids,
    canonicalize_agent_pair_ids,
)
from app.services.web_session_contract import apply_web_session_contract


def session_conversation_id(session: Any) -> str:
    """Return the canonical persisted conversation_id for a session."""
    return str(getattr(session, "id"))


async def _normalize_legacy_agent_pair_transcripts(
    db: AsyncSession,
    *,
    source_agent_id: uuid.UUID,
    target_agent_id: uuid.UUID,
    conversation_id: str,
) -> None:
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.conversation_id.in_(build_legacy_gateway_conversation_ids(source_agent_id, target_agent_id)))
        .values(conversation_id=conversation_id)
    )


async def create_chat_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
    source_channel: str,
    participant_id: uuid.UUID | None = None,
    peer_agent_id: uuid.UUID | None = None,
    external_conv_id: str | None = None,
    delivery_target_json: dict | None = None,
    created_at: datetime | None = None,
    session_id: uuid.UUID | None = None,
) -> ChatSession:
    """Create a ChatSession via the unified factory."""
    session = ChatSession(
        id=session_id or uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        title=title,
        source_channel=source_channel,
        participant_id=participant_id,
        peer_agent_id=peer_agent_id,
        external_conv_id=external_conv_id,
        delivery_target_json=delivery_target_json,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()
    return session


async def find_or_create_agent_pair_session(
    db: AsyncSession,
    *,
    source_agent_id: uuid.UUID,
    target_agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    source_name: str,
    target_name: str,
    source_participant_id: uuid.UUID | None = None,
) -> ChatSession:
    """Find or create the canonical agent-to-agent session for a pair."""
    session_agent_id, session_peer_id = canonicalize_agent_pair_ids(source_agent_id, target_agent_id)

    result = await db.execute(
        select(ChatSession).where(
            ChatSession.agent_id == session_agent_id,
            ChatSession.peer_agent_id == session_peer_id,
            ChatSession.source_channel == "agent",
        )
    )
    session = result.scalar_one_or_none()
    if session is not None:
        await _normalize_legacy_agent_pair_transcripts(
            db,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            conversation_id=session_conversation_id(session),
        )
        return session

    session = await create_chat_session(
        db,
        agent_id=session_agent_id,
        user_id=owner_user_id,
        title=f"{source_name} ↔ {target_name}"[:200],
        source_channel="agent",
        participant_id=source_participant_id,
        peer_agent_id=session_peer_id,
    )
    await _normalize_legacy_agent_pair_transcripts(
        db,
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        conversation_id=session_conversation_id(session),
    )
    return session


async def find_or_create_web_chat_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user: Any,
    requested_session_id: str | None = None,
    default_title: str | None = None,
) -> ChatSession:
    """Find or create the canonical web chat session for a user/agent pair."""
    session = None
    user_id = getattr(user, "id", None)

    if requested_session_id:
        try:
            requested_uuid = uuid.UUID(str(requested_session_id))
        except (TypeError, ValueError):
            requested_uuid = None
        if requested_uuid is not None:
            result = await db.execute(
                select(ChatSession).where(
                    ChatSession.id == requested_uuid,
                    ChatSession.agent_id == agent_id,
                    ChatSession.user_id == user_id,
                )
            )
            session = result.scalar_one_or_none()

    if session is None:
        result = await db.execute(
            select(ChatSession)
            .where(
                ChatSession.agent_id == agent_id,
                ChatSession.user_id == user_id,
                ChatSession.source_channel == "web",
            )
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
            .limit(1)
        )
        session = result.scalar_one_or_none()

    if session is None:
        now = datetime.now(timezone.utc)
        session = await create_chat_session(
            db,
            agent_id=agent_id,
            user_id=user_id,
            title=default_title or f"Session {now.strftime('%m-%d %H:%M')}",
            source_channel="web",
            created_at=now,
        )

    await apply_web_session_contract(db, session=session, agent_id=agent_id, user=user)
    return session
