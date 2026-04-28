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


def _can_auto_wake(objective: Any) -> bool:
    metadata = _objective_metadata(objective)
    if bool(metadata.get("requires_approval")):
        return False
    return str(getattr(objective, "status", "") or "open") in ACTIVE_OBJECTIVE_STATUSES


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
