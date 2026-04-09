"""Shared helper: find-or-create ChatSession by external channel conv_id.

Used by feishu.py, slack.py, discord_bot.py — eliminates in-process caches.
"""
import uuid as _uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession


async def find_or_create_channel_session(
    db: AsyncSession,
    agent_id: _uuid.UUID,
    user_id: _uuid.UUID,
    external_conv_id: str,
    source_channel: str,
    first_message_title: str,
    legacy_external_conv_ids: list[str] | None = None,
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
                .values(conversation_id=str(session.id), user_id=user_id)
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
                    if not legacy_session.title or legacy_session.title == "New Session":
                        legacy_session.title = first_message_title[:40]
                    await db.flush()
                    return legacy_session

        now = datetime.now(timezone.utc)
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title=first_message_title[:40],
            source_channel=source_channel,
            external_conv_id=external_conv_id,
            created_at=now,
        )
        db.add(session)
        await db.flush()  # populate session.id
    else:
        # Re-attribute old sessions that were stored under creator_id / wrong user
        if session.user_id != user_id:
            session.user_id = user_id

    return session
