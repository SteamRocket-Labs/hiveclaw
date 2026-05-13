"""Activity logger — simple async function to record agent actions."""

import uuid

from loguru import logger

from app.database import async_session
from app.models.activity_log import AgentActivityLog

_ACTION_TYPE_FALLBACKS = {
    "llm_error": "error",
    "tool_call_direct": "tool_call",
    "tool_call_approved": "tool_call",
}


def _fallback_action_type(action_type: str, exc: Exception) -> str | None:
    fallback = _ACTION_TYPE_FALLBACKS.get(action_type)
    if not fallback:
        return None
    if "invalid input value for enum activity_action_enum" not in str(exc).lower():
        return None
    return fallback


async def _insert_activity(
    *,
    agent_id: uuid.UUID,
    action_type: str,
    summary: str,
    detail: dict | None,
    related_id: uuid.UUID | None,
) -> None:
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
    except Exception:
        if db is not None:
            try:
                await db.rollback()
            except Exception as rollback_err:
                logger.warning(f"[ActivityLog] Rollback also failed: {rollback_err}")
        raise


async def log_activity(
    agent_id: uuid.UUID,
    action_type: str,
    summary: str,
    detail: dict | None = None,
    related_id: uuid.UUID | None = None,
) -> None:
    """Record an agent activity. Fire-and-forget, never raises."""
    try:
        await _insert_activity(
            agent_id=agent_id,
            action_type=action_type,
            summary=summary,
            detail=detail,
            related_id=related_id,
        )
    except Exception as e:
        fallback = _fallback_action_type(action_type, e)
        if fallback:
            fallback_detail = dict(detail or {})
            fallback_detail["activity_type_original"] = action_type
            fallback_detail["activity_type_fallback"] = fallback
            try:
                logger.warning(
                    "[ActivityLog] Falling back from {} to {} due to enum drift: {}",
                    action_type,
                    fallback,
                    e,
                )
                await _insert_activity(
                    agent_id=agent_id,
                    action_type=fallback,
                    summary=summary,
                    detail=fallback_detail,
                    related_id=related_id,
                )
                return
            except Exception as fallback_err:
                logger.error(f"[ActivityLog] Fallback logging also failed for {action_type}: {fallback_err}")
        logger.error(f"[ActivityLog] Failed to log {action_type}: {e}")
