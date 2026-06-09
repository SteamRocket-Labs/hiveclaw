"""Plan Mode detached-background handoff — a confirmed plan run as one ``once`` trigger.

Exec/automation CC-alignment (2026-06-08, §7): a confirmed plan whose user
explicitly asked for background / offline / "run it and notify me" execution is,
in the three-bucket model, just a **one-shot background task** — i.e. a ``once``
trigger that the trigger daemon fires shortly after, in the background, off the
live session.

So this target delegates to :func:`handoff_scheduled_trigger` with
``force_once=True``: it creates a single enabled ``once`` trigger stamped with the
load-bearing ``config.plan_id`` (the daemon's confirmed-plan backstop). No
net-new detached runtime, no silent ``skipped`` — the same governed trigger path
every other autonomous run uses.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.plan_mode_handoff import handoff_scheduled_trigger

logger = logging.getLogger(__name__)

DETACHED_TARGET = "detached_runtime_task"


async def detached_runtime_task_handoff(db: Any, plan: Any) -> dict[str, Any]:
    """Create a one-shot ``once`` background trigger for a confirmed detached plan."""
    logger.info(
        "plan_detached_handoff_once",
        extra={"plan_id": str(getattr(plan, "id", None)), "target": DETACHED_TARGET},
    )
    return await handoff_scheduled_trigger(plan, db=db, force_once=True)


def register_detached_runtime_task_handoff(service: Any) -> None:
    """Register the detached-background ``once`` handoff on a ``PlanModeService``."""
    service.register_handoff_handler(DETACHED_TARGET, detached_runtime_task_handoff)
