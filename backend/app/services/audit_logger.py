"""Helper to write audit log entries from background services."""

import json
import uuid
from datetime import datetime, timezone

from loguru import logger

from sqlalchemy import text

from app.database import async_session


async def write_audit_log(
    action: str,
    details: dict | None = None,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    """Write a single audit log entry using raw SQL.

    Uses raw SQL to avoid ORM foreign-key resolution issues when
    called from background tasks where not all models may be loaded.

    Args:
        action: Short action string, e.g. "supervision_tick", "schedule_execute".
        details: JSON-serialisable dict with extra info.
        agent_id: Optional agent UUID.
        user_id: Optional user UUID.
    """
    try:
        async with async_session() as db:
            await db.execute(
                text(
                    "INSERT INTO audit_logs (id, action, details, agent_id, user_id, created_at) "
                    "VALUES (:id, :action, :details, :agent_id, :user_id, :created_at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "action": action,
                    "details": json.dumps(details or {}, ensure_ascii=False, default=str),
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "created_at": datetime.now(timezone.utc),
                },
            )
            await db.commit()
    except Exception as e:
        # Never let audit logging break the caller
        logger.error(f"[audit_logger] WARNING: failed to write audit log: {e}")
