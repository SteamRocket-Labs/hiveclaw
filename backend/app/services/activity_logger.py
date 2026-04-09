"""Activity logger — simple async function to record agent actions."""

import uuid

from loguru import logger

from app.database import async_session
from app.models.activity_log import AgentActivityLog


async def log_activity(
    agent_id: uuid.UUID,
    action_type: str,
    summary: str,
    detail: dict | None = None,
    related_id: uuid.UUID | None = None,
) -> None:
    """Record an agent activity. Fire-and-forget, never raises."""
    db = None
    try:
        async with async_session() as db:
            db.add(AgentActivityLog(
                agent_id=agent_id,
                action_type=action_type,
                summary=summary[:500] if summary else "",
                detail_json=detail,
                related_id=related_id,
            ))
            await db.commit()
    except Exception as e:
        if db is not None:
            try:
                await db.rollback()
            except Exception as rollback_err:
                logger.warning(f"[ActivityLog] Rollback also failed: {rollback_err}")
        logger.error(f"[ActivityLog] Failed to log {action_type}: {e}")
