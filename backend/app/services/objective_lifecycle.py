"""Objective and trigger lifecycle policies for P5 execution reliability."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.objective import AgentObjective

ACTIVE_STATUSES = {"active", "open", "running"}
DEFAULT_FAILURE_BACKOFF_SECONDS = 60
MAX_FAILURE_BACKOFF_SECONDS = 60 * 60
RECOVERY_OBJECTIVE_FAILURE_THRESHOLD = 3


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def apply_stale_sla(objective: Any, *, now: datetime | None = None) -> bool:
    metadata = dict(getattr(objective, "metadata_json", None) or {})
    stale_after_hours = metadata.get("stale_after_hours")
    if not stale_after_hours:
        return False
    status = str(getattr(objective, "status", "") or "").lower()
    if status not in ACTIVE_STATUSES:
        return False
    updated_at = getattr(objective, "updated_at", None) or getattr(objective, "created_at", None)
    if updated_at is None:
        return False
    updated_at = _now(updated_at)
    current = _now(now)
    if current - updated_at <= timedelta(hours=float(stale_after_hours)):
        return False

    objective.status = "blocked"
    objective.blocked_reason = f"Objective stale SLA exceeded ({stale_after_hours}h without progress)."
    metadata["stale"] = {
        "reason": "stale_sla_exceeded",
        "stale_after_hours": stale_after_hours,
        "marked_at": current.isoformat(),
    }
    objective.metadata_json = metadata
    return True


def apply_trigger_failure_policy(trigger: Any, *, error: str, now: datetime | None = None) -> dict[str, Any]:
    current = _now(now)
    config = dict(getattr(trigger, "config", None) or {})
    failure_count = int(config.get("failure_count") or 0) + 1
    backoff_seconds = min(DEFAULT_FAILURE_BACKOFF_SECONDS * (2 ** max(failure_count - 1, 0)), MAX_FAILURE_BACKOFF_SECONDS)
    backoff_until = current + timedelta(seconds=backoff_seconds)
    config.update(
        {
            "failure_count": failure_count,
            "last_failure_at": current.isoformat(),
            "last_failure": str(error)[:1000],
            "backoff_until": backoff_until.isoformat(),
        }
    )
    trigger.config = config
    return {
        "failure_count": failure_count,
        "backoff_seconds": backoff_seconds,
        "backoff_until": backoff_until.isoformat(),
    }


def reset_trigger_failure_policy(trigger: Any) -> bool:
    config = dict(getattr(trigger, "config", None) or {})
    changed = False
    for key in ("failure_count", "last_failure_at", "last_failure", "backoff_until"):
        if key in config:
            config.pop(key, None)
            changed = True
    if changed:
        trigger.config = config
    return changed


def should_create_recovery_objective(trigger: Any) -> bool:
    config = dict(getattr(trigger, "config", None) or {})
    return int(config.get("failure_count") or 0) >= RECOVERY_OBJECTIVE_FAILURE_THRESHOLD


async def reconcile_all_objective_lifecycle() -> dict[str, Any]:
    """Mark objectives stale when they declare an explicit stale SLA."""
    try:
        async with async_session() as db:
            result = await db.execute(select(AgentObjective).where(AgentObjective.status.in_(tuple(ACTIVE_STATUSES))))
            objectives = list(result.scalars().all())
            changed = 0
            for objective in objectives:
                if apply_stale_sla(objective):
                    changed += 1
            if changed:
                await db.commit()
            return {"objectives_checked": len(objectives), "stale_marked": changed}
    except Exception as exc:
        logger.debug("[ObjectiveLifecycle] stale SLA reconcile failed: {}", exc)
        return {"objectives_checked": 0, "stale_marked": 0, "error": str(exc)}


async def create_recovery_objective_for_trigger_failure(
    db,
    *,
    agent_id: uuid.UUID,
    trigger: Any,
    error: str,
) -> Any | None:
    """Create a low-risk internal recovery objective after repeated trigger failures."""
    if not should_create_recovery_objective(trigger):
        return None
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return None
    from app.services.objective_intake import intake_internal_self_improvement

    key = f"recover_{str(getattr(trigger, 'name', 'trigger'))[:80]}"
    return await intake_internal_self_improvement(
        db,
        agent,
        description=(
            f"Recover autonomous trigger '{getattr(trigger, 'name', '')}' after repeated failures. "
            f"Latest error: {str(error)[:300]}"
        ),
        evidence={
            "source": "trigger_failure_backoff",
            "trigger_id": str(getattr(trigger, "id", "")),
            "trigger_name": getattr(trigger, "name", None),
            "failure_count": (getattr(trigger, "config", None) or {}).get("failure_count"),
        },
        objective_key=key,
        commit=False,
    )
