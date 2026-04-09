"""Notification service — unified entry point for sending in-app notifications."""

import uuid
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification


async def send_notification(
    db: AsyncSession,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str = "",
    link: Optional[str] = None,
    ref_id: Optional[uuid.UUID] = None,
) -> Notification:
    """Create and persist a notification for a user.

    Args:
        db: Database session.
        user_id: The user who should receive this notification.
        type: Notification category (approval_pending, plaza_comment, etc.).
        title: Short summary shown in the notification list.
        body: Extended detail text.
        link: Frontend route path for click-through navigation.
        ref_id: ID of the related object (approval, comment, etc.).
    """
    notif = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        link=link,
        ref_id=ref_id,
    )
    db.add(notif)
    await db.flush()
    logger.info(f"Notification [{type}] sent to user {user_id}: {title}")
    return notif
