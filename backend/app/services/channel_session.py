"""Shared helper: find-or-create ChatSession by external channel conv_id.

Used by feishu.py, slack.py, discord_bot.py — eliminates in-process caches.
"""

import uuid as _uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession


def _archived_external_conv_id(external_conv_id: str, session_id: _uuid.UUID) -> str:
    suffix = f"#archived:{session_id.hex[:12]}"
    return f"{external_conv_id[: max(0, 200 - len(suffix))]}{suffix}"


def _apply_channel_session_contract(session: ChatSession) -> None:
    session.session_kind = "human_chat"
    session.actor_type = "external_principal" if getattr(session, "external_principal_id", None) else "user"
    session.runtime_source = "channel_chat"
    session.visibility_scope = "direct_user"
    session.listed_surface = "chat"


async def find_or_create_channel_session(
    db: AsyncSession,
    agent_id: _uuid.UUID,
    user_id: _uuid.UUID | None,
    external_conv_id: str,
    source_channel: str,
    first_message_title: str,
    external_principal_id: _uuid.UUID | None = None,
    tenant_id: _uuid.UUID | None = None,
    legacy_external_conv_ids: list[str] | None = None,
    delivery_target: dict | None = None,
) -> ChatSession:
    """Find an existing ChatSession by (agent_id, external_conv_id), or create one.

    Relies on the UNIQUE constraint on (agent_id, external_conv_id) in the DB.
    """
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.agent_id == agent_id,
            ChatSession.external_conv_id == external_conv_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is not None and tenant_id and session.tenant_id is None:
        session.tenant_id = tenant_id
    if session is not None and external_principal_id is not None:
        existing_principal_id = getattr(session, "external_principal_id", None)
        if existing_principal_id not in (None, external_principal_id):
            raise ValueError("channel conversation is already bound to a different external principal")
        session.external_principal_id = external_principal_id

    if session is not None and legacy_external_conv_ids:
        for legacy_conv_id in legacy_external_conv_ids:
            legacy_result = await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.external_conv_id == legacy_conv_id,
                )
            )
            legacy_session = legacy_result.scalar_one_or_none()
            if not legacy_session or legacy_session.id == session.id:
                continue

            await db.execute(
                update(ChatMessage)
                .where(ChatMessage.conversation_id == str(legacy_session.id))
                .values(
                    conversation_id=str(session.id),
                    user_id=user_id,
                    external_principal_id=external_principal_id,
                    tenant_id=tenant_id,
                )
            )
            if legacy_session.last_message_at and (
                session.last_message_at is None or legacy_session.last_message_at > session.last_message_at
            ):
                session.last_message_at = legacy_session.last_message_at
            if (not session.title or session.title == "New Session") and legacy_session.title:
                session.title = legacy_session.title
            await db.delete(legacy_session)
            await db.flush()

    if session is None:
        if legacy_external_conv_ids:
            for legacy_conv_id in legacy_external_conv_ids:
                legacy_result = await db.execute(
                    select(ChatSession).where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.external_conv_id == legacy_conv_id,
                    )
                )
                legacy_session = legacy_result.scalar_one_or_none()
                if legacy_session:
                    legacy_session.external_conv_id = external_conv_id
                    legacy_session.user_id = user_id
                    legacy_session.external_principal_id = external_principal_id
                    if tenant_id and legacy_session.tenant_id is None:
                        legacy_session.tenant_id = tenant_id
                    if not legacy_session.title or legacy_session.title == "New Session":
                        legacy_session.title = first_message_title[:40]
                    if delivery_target:
                        legacy_session.delivery_target_json = delivery_target
                    _apply_channel_session_contract(legacy_session)
                    await db.flush()
                    return legacy_session

        now = datetime.now(timezone.utc)
        session = ChatSession(
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            external_principal_id=external_principal_id,
            title=first_message_title[:40],
            source_channel=source_channel,
            session_kind="human_chat",
            actor_type="user",
            runtime_source="channel_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
            external_conv_id=external_conv_id,
            delivery_target_json=delivery_target,
            created_at=now,
        )
        db.add(session)
        await db.flush()  # populate session.id
    else:
        # Re-attribute old sessions that were stored under creator_id / wrong user
        if session.user_id != user_id:
            session.user_id = user_id
        if external_principal_id is not None:
            session.external_principal_id = external_principal_id
        if delivery_target:
            session.delivery_target_json = delivery_target

    _apply_channel_session_contract(session)
    return session


async def start_new_channel_session(
    db: AsyncSession,
    agent_id: _uuid.UUID,
    user_id: _uuid.UUID | None,
    external_conv_id: str,
    source_channel: str,
    title: str = "New Session",
    external_principal_id: _uuid.UUID | None = None,
    tenant_id: _uuid.UUID | None = None,
    legacy_external_conv_ids: list[str] | None = None,
    delivery_target: dict | None = None,
) -> ChatSession:
    """Start a fresh channel session by releasing the active external conv id."""
    candidate_conv_ids = [external_conv_id, *(legacy_external_conv_ids or [])]
    existing_result = await db.execute(
        select(ChatSession).where(
            ChatSession.agent_id == agent_id,
            ChatSession.external_conv_id.in_(candidate_conv_ids),
        )
    )
    existing_sessions = list(existing_result.scalars().all())
    for existing in existing_sessions:
        if tenant_id and existing.tenant_id is None:
            existing.tenant_id = tenant_id
        if existing.external_conv_id:
            existing.external_conv_id = _archived_external_conv_id(existing.external_conv_id, existing.id)

    now = datetime.now(timezone.utc)
    session = ChatSession(
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        external_principal_id=external_principal_id,
        title=(title or "New Session")[:40],
        source_channel=source_channel,
        session_kind="human_chat",
        actor_type="user",
        runtime_source="channel_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        external_conv_id=external_conv_id,
        delivery_target_json=delivery_target,
        created_at=now,
    )
    db.add(session)
    await db.flush()
    if delivery_target is not None:
        delivery_target["session_id"] = str(session.id)
        session.delivery_target_json = delivery_target
    _apply_channel_session_contract(session)
    return session
