"""Notification API — list, count, mark-read, and tenant broadcast."""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.notification import Notification
from app.models.user import User
from app.services.notification_service import send_notification

router = APIRouter(tags=["notifications"])


class BroadcastNotificationIn(BaseModel):
    title: str
    body: str = ""


@router.get("/notifications")
async def list_notifications(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List notifications for the current user, newest first."""
    query = select(Notification).where(Notification.user_id == current_user.id)
    if unread_only:
        query = query.where(Notification.is_read == False)  # noqa: E712
    query = query.order_by(Notification.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    notifications = result.scalars().all()
    return [
        {
            "id": str(n.id),
            "type": n.type,
            "title": n.title,
            "body": n.body,
            "link": n.link,
            "ref_id": str(n.ref_id) if n.ref_id else None,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notifications
    ]


@router.get("/notifications/unread-count")
async def get_unread_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the number of unread notifications for the current user."""
    result = await db.execute(
        select(func.count(Notification.id)).where(
            Notification.user_id == current_user.id,
            Notification.is_read == False,  # noqa: E712
        )
    )
    return {"unread_count": result.scalar() or 0}


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a single notification as read."""
    await db.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.user_id == current_user.id)
        .values(is_read=True)
    )
    await db.commit()
    return {"ok": True}


@router.post("/notifications/read-all")
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark all notifications as read for the current user."""
    await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.is_read == False)  # noqa: E712
        .values(is_read=True)
    )
    await db.commit()
    return {"ok": True}


@router.post("/notifications/broadcast")
async def broadcast_notifications(
    data: BroadcastNotificationIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send an in-app broadcast notification to all active users in the current tenant."""
    if not current_user.tenant_id:
        return {"users_notified": 0, "agents_notified": 0}

    users_result = await db.execute(
        select(User).where(
            User.tenant_id == current_user.tenant_id,
            User.is_active == True,  # noqa: E712
        )
    )
    users = users_result.scalars().all()
    for user in users:
        await send_notification(
            db=db,
            user_id=user.id,
            type="broadcast",
            title=data.title,
            body=data.body or "",
        )

    agent_count_result = await db.execute(
        select(func.count(Agent.id)).where(Agent.tenant_id == current_user.tenant_id)
    )
    await db.commit()
    return {
        "users_notified": len(users),
        "agents_notified": agent_count_result.scalar_one_or_none() or 0,
    }
