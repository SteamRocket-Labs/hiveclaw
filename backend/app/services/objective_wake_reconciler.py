"""Maintain wake policies for active objectives."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.objective import AgentObjective
from app.models.trigger import AgentTrigger
from app.services import plan_mode_core
from app.services.focus_state import normalize_focus_task_id

ACTIVE_OBJECTIVE_STATUSES = {"active", "open", "running"}


def _objective_metadata(objective: Any) -> dict[str, Any]:
    return dict(getattr(objective, "metadata_json", None) or {})


def _default_once_config(now: datetime | None = None) -> dict[str, Any]:
    fire_at = (now or datetime.now(timezone.utc)) + timedelta(seconds=30)
    return {"at": fire_at.isoformat()}


def build_objective_trigger_payload(objective: Any, *, now: datetime | None = None) -> dict[str, Any]:
    key = normalize_focus_task_id(str(getattr(objective, "objective_key", "") or "objective"))
    metadata = _objective_metadata(objective)
    wake_policy = dict(metadata.get("wake_policy") or {})
    trigger_type = str(wake_policy.get("type") or "once")
    config = dict(wake_policy.get("config") or {})
    if trigger_type == "once" and not config.get("at"):
        if config.get("delay_seconds"):
            fire_at = (now or datetime.now(timezone.utc)) + timedelta(seconds=int(config["delay_seconds"]))
            config = {"at": fire_at.isoformat()}
        else:
            config = _default_once_config(now)
    config["trigger_class"] = "objective_task"
    config["objective_id"] = str(getattr(objective, "id"))

    success = str(getattr(objective, "success_criteria", "") or "").strip()
    reason = (
        f"Objective: {getattr(objective, 'description', '')}\n"
        f"Objective ID: {getattr(objective, 'id')}\n"
        f"Focus Ref: {key}\n"
    )
    if success:
        reason += f"Success criteria: {success}\n"
    reason += (
        "Work this objective in its stable objective session. "
        "When genuinely complete, call complete_objective with concrete evidence; "
        "if blocked, call update_objective with status='blocked' and a blocked_reason."
    )
    return {
        "name": f"objective_{key}"[:100],
        "type": trigger_type,
        "config": config,
        "reason": reason,
        "focus_ref": key,
    }


def objective_has_enabled_wake(objective: Any, triggers: list[Any]) -> bool:
    objective_id = str(getattr(objective, "id", "") or "")
    objective_agent_id = str(getattr(objective, "agent_id", "") or "")
    key = normalize_focus_task_id(str(getattr(objective, "objective_key", "") or ""))
    for trigger in triggers:
        trigger_agent_id = str(getattr(trigger, "agent_id", "") or "")
        if objective_agent_id and trigger_agent_id and trigger_agent_id != objective_agent_id:
            continue
        if not bool(getattr(trigger, "is_enabled", False)):
            continue
        config = getattr(trigger, "config", None) or {}
        trigger_class = str(config.get("trigger_class") or "").strip()
        if trigger_class and trigger_class != "objective_task":
            continue
        if str(config.get("objective_id") or "").strip() == objective_id:
            return True
        focus_ref = str(getattr(trigger, "focus_ref", "") or "").strip()
        if focus_ref and normalize_focus_task_id(focus_ref) == key:
            return True
    return False


def _objective_autonomy_class(objective: Any) -> str | None:
    metadata = _objective_metadata(objective)
    value = metadata.get("autonomy_class")
    return str(value).strip() if value is not None else None


def auto_wake_exempt_reason(objective: Any) -> str | None:
    """Why this objective may have an enabled wake auto-created (§9.0), if at all.

    Returns a stable reason string when the objective traces to a confirmed plan,
    carries an explicit cutover exemption, or is platform-internal self-evolution.
    Returns ``None`` when the objective must NOT be auto-woken — the
    conversation-intent bypass the Plan Mode backstop closes.
    """
    return plan_mode_core.objective_wake_exempt_reason(
        objective_metadata=_objective_metadata(objective),
        autonomy_class=_objective_autonomy_class(objective),
    )


def _can_auto_wake(objective: Any) -> bool:
    """Whether the reconciler may auto-create an *enabled* wake for an objective.

    Two gates, both must pass:

    1. **Status / approval** — objective is active and not flagged
       ``requires_approval`` (the pre-existing P5 gate).
    2. **Plan Mode backstop (§9.0)** — the objective must be plan-exempt
       (confirmed-plan provenance, explicit cutover exemption, or
       platform-internal self-evolution). A bare active objective born from a
       conversation intent is fail-closed: no enabled trigger without a
       confirmed plan.
    """
    metadata = _objective_metadata(objective)
    if bool(metadata.get("requires_approval")):
        return False
    if str(getattr(objective, "status", "") or "open") not in ACTIVE_OBJECTIVE_STATUSES:
        return False
    return auto_wake_exempt_reason(objective) is not None


def _existing_trigger_for_name(triggers: list[Any], *, agent_id: uuid.UUID, name: str) -> Any | None:
    agent_id_str = str(agent_id)
    for trigger in triggers:
        trigger_agent_id = str(getattr(trigger, "agent_id", "") or "")
        if trigger_agent_id and trigger_agent_id != agent_id_str:
            continue
        if str(getattr(trigger, "name", "") or "") == name:
            return trigger
    return None


async def reconcile_agent_objective_wake_policies(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    commit: bool = True,
) -> dict[str, Any]:
    result = await db.execute(
        select(AgentObjective).where(
            AgentObjective.agent_id == agent_id,
            AgentObjective.status.in_(tuple(ACTIVE_OBJECTIVE_STATUSES)),
        )
    )
    objectives = list(result.scalars().all())
    trigger_result = await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id))
    triggers = list(trigger_result.scalars().all())

    created = 0
    updated = 0
    skipped = 0
    for objective in objectives:
        if not _can_auto_wake(objective):
            skipped += 1
            continue
        if objective_has_enabled_wake(objective, triggers):
            continue
        payload = build_objective_trigger_payload(objective)
        existing_trigger = _existing_trigger_for_name(triggers, agent_id=agent_id, name=payload["name"])
        if existing_trigger is not None:
            existing_trigger.type = payload["type"]
            existing_trigger.config = payload["config"]
            existing_trigger.reason = payload["reason"]
            existing_trigger.focus_ref = payload["focus_ref"]
            existing_trigger.is_enabled = True
            updated += 1
            continue
        trigger = AgentTrigger(
            agent_id=agent_id,
            name=payload["name"],
            type=payload["type"],
            config=payload["config"],
            reason=payload["reason"],
            focus_ref=payload["focus_ref"],
        )
        db.add(trigger)
        triggers.append(trigger)
        created += 1
    if created or updated:
        await db.flush()
    if commit and (created or updated):
        await db.commit()
    return {
        "agent_id": str(agent_id),
        "objectives": len(objectives),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


async def reconcile_all_objective_wake_policies() -> dict[str, Any]:
    try:
        async with async_session() as db:
            result = await db.execute(
                select(AgentObjective.agent_id).where(AgentObjective.status.in_(tuple(ACTIVE_OBJECTIVE_STATUSES)))
            )
            agent_ids = sorted({row[0] for row in result.all()})
            created = 0
            updated = 0
            for agent_id in agent_ids:
                report = await reconcile_agent_objective_wake_policies(db, agent_id=agent_id, commit=False)
                created += int(report.get("created") or 0)
                updated += int(report.get("updated") or 0)
            if created or updated:
                await db.commit()
            return {"agents_checked": len(agent_ids), "created": created, "updated": updated}
    except Exception as exc:
        logger.debug("[ObjectiveWake] reconcile all failed: {}", exc)
        return {"agents_checked": 0, "created": 0, "updated": 0, "error": str(exc)}
