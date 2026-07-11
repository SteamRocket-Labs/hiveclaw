"""Shared agent-pair session and participant ledger helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.session_identifiers import (
    build_agent_pair_session_id,
    canonicalize_agent_pair_ids,
)


def _uuid_or_none(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        return None


def session_conversation_id(session: ChatSession) -> str:
    """Return the canonical conversation id used by transcript ledgers."""
    return str(session.id)


def _normalize_agent_pair_session_contract(
    session: ChatSession,
    *,
    source_agent_id: uuid.UUID | str,
    target_agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None = None,
    owner_user_id: uuid.UUID | str,
    source_agent_name: str,
    target_agent_name: str,
    source_participant_id: uuid.UUID | None = None,
    root_session_id: uuid.UUID | str | None = None,
) -> None:
    """Repair any existing pair session into the canonical A2A read-model shape."""
    source_uuid = uuid.UUID(str(source_agent_id))
    target_uuid = uuid.UUID(str(target_agent_id))
    tenant_uuid = uuid.UUID(str(tenant_id)) if tenant_id else None
    owner_uuid = uuid.UUID(str(owner_user_id))
    root_session_uuid = _uuid_or_none(root_session_id)
    session_agent_id, peer_agent_id = canonicalize_agent_pair_ids(source_uuid, target_uuid)

    session.agent_id = session_agent_id
    session.peer_agent_id = peer_agent_id
    if tenant_uuid is not None:
        session.tenant_id = tenant_uuid
    session.user_id = owner_uuid
    session.parent_session_id = root_session_uuid
    session.root_session_id = root_session_uuid
    if not getattr(session, "title", None):
        session.title = f"{source_agent_name} ↔ {target_agent_name}"
    session.source_channel = "agent"
    session.session_kind = "agent_chat"
    session.actor_type = "agent"
    session.runtime_source = "agent_to_agent_chat"
    session.visibility_scope = "agent_owner"
    session.listed_surface = "chat"
    if source_participant_id is not None:
        session.participant_id = source_participant_id

    metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    metadata.update(
        {
            "source": "agent",
            "interaction_type": "agent_message",
            "a2a_session": True,
            "from_agent_id": str(source_uuid),
            "to_agent_id": str(target_uuid),
            "from_agent_name": source_agent_name,
            "to_agent_name": target_agent_name,
            "root_session_id": str(root_session_id) if root_session_id else None,
            "root_user_id": str(owner_uuid),
        }
    )
    session.transcript_metadata_json = metadata


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
    root_session_id: uuid.UUID | str | None = None,
) -> ChatSession:
    """Find or create the durable ChatSession for an agent pair."""
    source_uuid = uuid.UUID(str(source_agent_id))
    target_uuid = uuid.UUID(str(target_agent_id))
    tenant_uuid = uuid.UUID(str(tenant_id)) if tenant_id else None
    owner_uuid = uuid.UUID(str(owner_user_id))
    root_session_uuid = _uuid_or_none(root_session_id)
    session_uuid = build_agent_pair_session_id(
        source_uuid,
        target_uuid,
        owner_user_id=owner_uuid,
        root_session_id=root_session_id,
    )
    session_agent_id, peer_agent_id = canonicalize_agent_pair_ids(source_uuid, target_uuid)

    result = await db.execute(select(ChatSession).where(ChatSession.id == session_uuid))
    session = result.scalar_one_or_none()
    if session is None:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.source_channel == "agent",
                ChatSession.agent_id == session_agent_id,
                ChatSession.peer_agent_id == peer_agent_id,
                ChatSession.user_id == owner_uuid,
                ChatSession.root_session_id == root_session_uuid,
            )
        )
        session = result.scalar_one_or_none()
    if session is None:
        session = ChatSession(
            id=session_uuid,
            agent_id=session_agent_id,
            tenant_id=tenant_uuid,
            user_id=owner_uuid,
            title=f"{source_agent_name} ↔ {target_agent_name}",
            source_channel="agent",
            session_kind="agent_chat",
            actor_type="agent",
            runtime_source="agent_to_agent_chat",
            visibility_scope="agent_owner",
            listed_surface="chat",
            participant_id=source_participant_id,
            peer_agent_id=peer_agent_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(session)
        flush = getattr(db, "flush", None)
        if flush:
            await flush()

    _normalize_agent_pair_session_contract(
        session,
        source_agent_id=source_uuid,
        target_agent_id=target_uuid,
        tenant_id=tenant_uuid,
        owner_user_id=owner_uuid,
        source_agent_name=source_agent_name,
        target_agent_name=target_agent_name,
        source_participant_id=source_participant_id,
        root_session_id=root_session_id,
    )
    return session
