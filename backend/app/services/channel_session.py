"""Shared helper: find-or-create ChatSession by external channel conv_id.

Used by feishu.py, slack.py, discord_bot.py — eliminates in-process caches.
"""
import uuid as _uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.services.session_service import create_chat_session


async def find_or_create_channel_session(
    db: AsyncSession,
    agent_id: _uuid.UUID,
    user_id: _uuid.UUID,
    external_conv_id: str,
    source_channel: str,
    first_message_title: str,
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

    if session is None:
        now = datetime.now(timezone.utc)
        session = await create_chat_session(
            db,
            agent_id=agent_id,
            user_id=user_id,
            title=first_message_title[:40],
            source_channel=source_channel,
            external_conv_id=external_conv_id,
            delivery_target_json=delivery_target,
            created_at=now,
        )
    else:
        # Re-attribute old sessions that were stored under creator_id / wrong user
        if session.user_id != user_id:
            session.user_id = user_id
        if delivery_target:
            session.delivery_target_json = delivery_target

    return session
