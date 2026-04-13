"""Schedule compatibility helpers on top of the trigger runtime."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import AgentSchedule
from app.models.trigger import AgentTrigger

SCHEDULE_TRIGGER_SURFACE = "schedule_api"


def is_schedule_compat_trigger(trigger: AgentTrigger | object) -> bool:
    cfg = getattr(trigger, "config", None) or {}
    return getattr(trigger, "type", None) == "cron" and cfg.get("_surface") == SCHEDULE_TRIGGER_SURFACE


def compute_next_run(cron_expr: str, *, after: datetime | None = None, tz_name: str | None = None) -> datetime | None:
    """Compute the next cron fire time in UTC."""
    try:
        base = after or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(tz_name or "UTC")
        local_base = base.astimezone(tz)
        next_run = croniter(cron_expr, local_base).get_next(datetime)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=tz)
        return next_run.astimezone(timezone.utc)
    except Exception:
        return None


def build_schedule_trigger_config(
    *,
    instruction: str,
    cron_expr: str,
    delivery_target_json: dict | None,
    created_by: uuid.UUID | str | None,
    existing_config: dict | None = None,
) -> dict:
    """Build the canonical trigger config for the schedule API surface."""
    cfg = dict(existing_config or {})
    cfg["_surface"] = SCHEDULE_TRIGGER_SURFACE
    cfg["expr"] = cron_expr
    cfg["instruction"] = instruction
    if created_by:
        cfg["created_by"] = str(created_by)
    else:
        cfg.pop("created_by", None)
    if delivery_target_json is None:
        cfg.pop("delivery_target_json", None)
    else:
        cfg["delivery_target_json"] = delivery_target_json
    return cfg


def schedule_response_payload(trigger: AgentTrigger | object, *, creator_username: str | None = None) -> dict:
    """Map a compat trigger into the legacy schedule response shape."""
    cfg = getattr(trigger, "config", None) or {}
    expr = str(cfg.get("expr") or "")
    created_by = None
    created_by_raw = cfg.get("created_by")
    if created_by_raw:
        try:
            created_by = uuid.UUID(str(created_by_raw))
        except (TypeError, ValueError):
            created_by = None
    next_run_at = None
    if getattr(trigger, "is_enabled", False) and expr:
        next_run_at = compute_next_run(
            expr,
            after=getattr(trigger, "last_fired_at", None) or getattr(trigger, "created_at", None),
            tz_name=cfg.get("timezone"),
        )
    return {
        "id": getattr(trigger, "id"),
        "agent_id": getattr(trigger, "agent_id"),
        "name": getattr(trigger, "name"),
        "instruction": str(cfg.get("instruction") or getattr(trigger, "reason", "") or ""),
        "cron_expr": expr,
        "is_enabled": bool(getattr(trigger, "is_enabled", False)),
        "last_run_at": getattr(trigger, "last_fired_at", None),
        "next_run_at": next_run_at,
        "run_count": int(getattr(trigger, "fire_count", 0) or 0),
        "created_by": created_by,
        "creator_username": creator_username,
        "delivery_target_json": cfg.get("delivery_target_json"),
        "created_at": getattr(trigger, "created_at", None),
    }


def mark_schedule_manual_pending(config: dict | None) -> dict:
    """Mark a compat trigger for immediate one-shot execution by trigger_daemon."""
    cfg = dict(config or {})
    cfg["_manual_pending"] = True
    cfg["_manual_request_id"] = uuid.uuid4().hex
    cfg["_manual_requested_at"] = datetime.now(timezone.utc).isoformat()
    return cfg


def clear_schedule_manual_pending(config: dict | None) -> dict:
    cfg = dict(config or {})
    cfg["_manual_pending"] = False
    cfg["_manual_request_id"] = None
    cfg["_manual_requested_at"] = None
    return cfg


def schedule_manual_evaluation(trigger: AgentTrigger | object) -> dict | None:
    """Return a manual fire event key when a compat trigger is pending manual execution."""
    if not is_schedule_compat_trigger(trigger):
        return None
    cfg = getattr(trigger, "config", None) or {}
    if not cfg.get("_manual_pending"):
        return None
    request_id = str(cfg.get("_manual_request_id") or "manual")
    return {"event_key": f"manual:{getattr(trigger, 'id')}:{request_id}"}


def build_schedule_activity_entries(triggers: list[AgentTrigger | object], reply: str | None) -> list[dict]:
    """Build legacy schedule activity log entries for compat triggers."""
    entries: list[dict] = []
    reply_text = reply or ""
    for trigger in triggers:
        if not is_schedule_compat_trigger(trigger):
            continue
        cfg = getattr(trigger, "config", None) or {}
        instruction = str(cfg.get("instruction") or getattr(trigger, "reason", "") or getattr(trigger, "name", ""))
        entries.append(
            {
                "action_type": "schedule_run",
                "summary": f"定时任务执行: {instruction[:60]}",
                "detail": {
                    "schedule_id": str(getattr(trigger, "id")),
                    "instruction": instruction,
                    "reply": reply_text[:500],
                },
            }
        )
    return entries


async def migrate_legacy_schedules(db: AsyncSession, agent_id: uuid.UUID) -> int:
    """Promote legacy AgentSchedule rows into compat AgentTrigger rows in-place."""
    legacy_result = await db.execute(select(AgentSchedule).where(AgentSchedule.agent_id == agent_id))
    legacy_schedules = list(legacy_result.scalars().all())
    if not legacy_schedules:
        return 0

    trigger_result = await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id))
    existing_triggers = {trigger.id: trigger for trigger in trigger_result.scalars().all()}

    migrated = 0
    for legacy in legacy_schedules:
        compat_config = build_schedule_trigger_config(
            instruction=legacy.instruction,
            cron_expr=legacy.cron_expr,
            delivery_target_json=legacy.delivery_target_json,
            created_by=legacy.created_by,
        )
        trigger = existing_triggers.get(legacy.id)
        if trigger is None:
            trigger = AgentTrigger(
                id=legacy.id,
                agent_id=legacy.agent_id,
                name=legacy.name,
                type="cron",
                config=compat_config,
                reason=legacy.instruction,
                is_enabled=legacy.is_enabled,
                last_fired_at=legacy.last_run_at,
                fire_count=legacy.run_count or 0,
                created_at=legacy.created_at,
            )
            db.add(trigger)
        else:
            trigger.name = legacy.name
            trigger.type = "cron"
            trigger.config = compat_config
            trigger.reason = legacy.instruction
            trigger.is_enabled = legacy.is_enabled
            trigger.last_fired_at = legacy.last_run_at
            trigger.fire_count = legacy.run_count or 0
            trigger.created_at = legacy.created_at
        await db.delete(legacy)
        migrated += 1
    return migrated
