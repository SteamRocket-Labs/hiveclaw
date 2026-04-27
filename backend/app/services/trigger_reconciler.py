"""Read-only focus reconciliation actions for autonomous trigger hygiene."""

from __future__ import annotations

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.trigger import AgentTrigger
from app.services import autonomous_audit
from app.services import objective_service
from app.services.focus_state import extract_completed_focus_refs, normalize_focus_task_id


async def reconcile_completed_focus_triggers(agent_id: uuid.UUID) -> dict[str, Any]:
    """Disable enabled triggers whose focus_ref points at completed objectives."""
    async with async_session() as db:
        sync_error = None
        try:
            await objective_service.sync_agent_focus_file_to_objectives(
                db,
                type("AgentRef", (), {"id": agent_id, "tenant_id": None})(),
                write_projection=True,
                commit=False,
            )
        except Exception as exc:
            sync_error = str(exc)
            logger.debug("[TriggerReconciler] Objective sync failed for {}: {}", agent_id, exc)

        try:
            completed_refs = await objective_service.completed_objective_refs(db, agent_id)
        except Exception as exc:
            sync_error = sync_error or str(exc)
            logger.debug("[TriggerReconciler] Objective completion lookup failed for {}: {}", agent_id, exc)
            completed_refs = set()

        if not completed_refs:
            focus_text, focus_error = autonomous_audit._read_focus_text(agent_id)
            if focus_error:
                return {
                    "agent_id": str(agent_id),
                    "checked": 0,
                    "cancelled": 0,
                    "error": focus_error,
                }
            completed_refs = extract_completed_focus_refs(focus_text or "")

        if not completed_refs:
            return {
                "agent_id": str(agent_id),
                "checked": 0,
                "cancelled": 0,
                "error": sync_error,
            }

        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.is_enabled.is_(True),
                AgentTrigger.focus_ref.isnot(None),
            )
        )
        triggers = result.scalars().all()

        cancelled_names: list[str] = []
        for trigger in triggers:
            focus_ref = str(getattr(trigger, "focus_ref", "") or "")
            if normalize_focus_task_id(focus_ref) not in completed_refs:
                continue
            trigger.is_enabled = False
            cancelled_names.append(str(getattr(trigger, "name", "")))
            logger.info(
                "[TriggerReconciler] Disabled trigger '{}' for completed focus_ref '{}' (agent={})",
                getattr(trigger, "name", ""),
                focus_ref,
                agent_id,
            )

        if cancelled_names:
            await db.commit()

    return {
        "agent_id": str(agent_id),
        "checked": len(triggers),
        "cancelled": len(cancelled_names),
        "cancelled_triggers": cancelled_names,
        "error": sync_error,
    }


async def reconcile_all_completed_focus_triggers() -> dict[str, Any]:
    """Run completed-focus reconciliation for every agent with enabled focus triggers."""
    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger.agent_id)
            .where(
                AgentTrigger.is_enabled.is_(True),
                AgentTrigger.focus_ref.isnot(None),
            )
            .distinct()
        )
        agent_ids = result.scalars().all()

    reports = []
    total_cancelled = 0
    for agent_id in agent_ids:
        report = await reconcile_completed_focus_triggers(agent_id)
        reports.append(report)
        total_cancelled += int(report.get("cancelled") or 0)

    return {
        "agents_checked": len(agent_ids),
        "cancelled": total_cancelled,
        "reports": reports,
    }
