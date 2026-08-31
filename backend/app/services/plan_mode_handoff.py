"""Plan Mode ``scheduled_trigger`` handoff — confirmed plan -> enabled cron trigger.

Exec/automation CC-alignment (2026-06-08): a confirmed *recurring* plan
(``intent_type=autonomous_wake``) sets up a schedule. Claude Code has no
``objective`` concept — a schedule is just a trigger. So this handoff creates an
:class:`AgentTrigger` **directly** from the plan's ``wake_policy``; the old
intermediate ``AgentObjective`` row (and the ``objective_trigger`` target) is gone.

It plugs into :class:`PlanModeService` via :func:`register_scheduled_trigger_handoff`
— the service awaits :func:`scheduled_trigger_handoff_handler` after a successful
confirmation.

Design:

* **The ``config.plan_id`` contract is load-bearing.** The trigger-daemon backstop
  (``trigger_preflight._plan_gate_block_for_triggers``) treats an autonomous
  trigger as legitimate iff it can prove it came from a confirmed plan. The proof
  is ``config.plan_id`` pointing at a ``confirmed`` :class:`AgentPlanRequest`.
  Without it the backstop would quarantine the trigger this handoff just created.
* **No objective.** The trigger is created with ``config.trigger_class="scheduled_job"``
  (the non-objective autonomous class), keyed by ``config.plan_id`` so re-running
  the handoff for the same plan updates the existing trigger instead of duplicating.
* **Atomic + idempotent.** The trigger is written in a single transaction owned by
  :class:`PlanModeService`. Re-running for the same plan updates the existing
  trigger (matched on ``config.plan_id``) instead of creating a duplicate.
* **Fail loud.** Invalid plan state or a missing agent raises :class:`HandoffError`;
  :class:`PlanModeService` catches it and records ``handoff_status="failed"``
  without mutating ``status`` (§13). No partial silent success.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.plan_authorization_lease import require_active_plan_authorization
from app.models.agent import Agent
from app.models.trigger import AgentTrigger
from app.services.trigger_resource_authority import preserve_trigger_authority, strip_trigger_runtime_config

logger = logging.getLogger(__name__)

HANDOFF_TARGET = "scheduled_trigger"

#: Schedule types a confirmed plan may set up. Mirrors the daemon's
#: time-driven bucket (cron/interval/once); anything else falls back to ``cron``.
_SCHEDULED_TRIGGER_TYPES = frozenset({"cron", "interval", "once"})


class HandoffError(Exception):
    """A handoff that could not be completed; surfaced so the caller records
    ``handoff_status="failed"`` rather than treating it as success."""


async def _load_agent(db: Any, agent_id: uuid.UUID | str) -> Agent | None:
    """Load the owning agent (split out so tests can stub it)."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


def _plan_title(plan: Any) -> str:
    plan_json = plan.plan_json or {}
    return str(plan_json.get("title") or plan.original_request or "Planned task").strip()


def _trigger_name(plan: Any) -> str:
    """Stable, human-readable trigger name. Idempotency keys off ``config.plan_id``,
    not the name, so the slug only needs to be readable."""
    slug = re.sub(r"[^a-z0-9]+", "_", _plan_title(plan).lower()).strip("_")
    base = slug or "plan"
    return f"plan_{base}"[:100]


def _trigger_payload_from_plan(plan: Any, *, force_once: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Translate the confirmed plan's ``wake_policy`` into a trigger payload.

    Accepts both shapes the codebase produces: ``wake_policy.config`` (nested) and
    top-level schedule keys (``expr`` / ``at`` / ``minutes``) on ``wake_policy``
    itself (the :func:`build_plan_skeleton` shape).

    ``force_once`` makes the trigger a one-shot background task regardless of the
    plan's ``wake_policy`` — this is how a *detached* ("run it and notify me")
    confirmed plan becomes a single ``once`` trigger the daemon fires in the
    background.
    """
    plan_json = plan.plan_json or {}
    wake_policy = dict(plan_json.get("wake_policy") or {})

    trigger_type = "once" if force_once else str(wake_policy.get("type") or "cron")
    if trigger_type not in _SCHEDULED_TRIGGER_TYPES:
        trigger_type = "cron"

    config: dict[str, Any] = strip_trigger_runtime_config(wake_policy.get("config"))
    # Promote top-level schedule keys (skeleton shape) into config.
    for key in ("expr", "at", "minutes", "interval_min", "delay_seconds"):
        if key in wake_policy and key not in config:
            config[key] = wake_policy[key]
    if wake_policy.get("timezone") and "timezone" not in config:
        config["timezone"] = wake_policy["timezone"]

    # A ``once`` schedule needs a concrete fire time.
    if trigger_type == "once" and not config.get("at"):
        delay = int(config.pop("delay_seconds", 0) or 0) or 30
        config["at"] = ((now or datetime.now(timezone.utc)) + timedelta(seconds=delay)).isoformat()

    config["trigger_class"] = "scheduled_job"
    config["plan_id"] = str(plan.id)  # load-bearing backstop contract
    config["plan_version"] = plan.plan_version
    config["plan_hash"] = plan.plan_hash
    config["plan_authorization"] = require_active_plan_authorization(plan)

    reason = (
        f"Confirmed plan: {_plan_title(plan)}\n"
        f"Plan ID: {plan.id}\n"
        "Execute the confirmed plan when this trigger fires. The full confirmed plan "
        "is injected as context via its plan_id."
    )
    return {"name": _trigger_name(plan), "type": trigger_type, "config": config, "reason": reason}


async def handoff_scheduled_trigger(plan: Any, *, db: Any | None = None, force_once: bool = False) -> dict[str, Any]:
    """Create/update the enabled trigger for a confirmed plan.

    ``force_once`` turns it into a one-shot background task (the *detached* path).

    Returns the audit payload recorded on ``handoff_payload`` (created trigger id).

    Raises:
        HandoffError: if the plan is not ``confirmed`` or its agent is missing.
    """
    if getattr(plan, "status", None) != "confirmed":
        raise HandoffError(
            f"scheduled_trigger handoff requires a confirmed plan (status={getattr(plan, 'status', None)!r})"
        )

    if db is not None:
        trigger = await _handoff_scheduled_trigger_in_session(db, plan, force_once=force_once)
        return _handoff_payload(plan, trigger)

    # Bare branch (no caller session): reads the agent (RLS-policied) to author
    # its wake trigger, so pin the GUC to the plan's tenant — under enforced
    # (non-owner) RLS the agent row is otherwise invisible and the handoff fails.
    tenant_id = await resolve_tenant_for_agent(getattr(plan, "agent_id", None))
    async with tenant_scoped_session(tenant_id) as owned_db:
        try:
            trigger = await _handoff_scheduled_trigger_in_session(owned_db, plan, force_once=force_once)
            await owned_db.commit()
        except HandoffError:
            await owned_db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as typed error, not swallowed
            await owned_db.rollback()
            logger.warning(
                "plan_scheduled_trigger_handoff_failed",
                extra={"plan_id": str(plan.id), "error": str(exc)},
            )
            raise HandoffError(f"scheduled_trigger handoff failed: {exc}") from exc
        return _handoff_payload(plan, trigger)


async def _handoff_scheduled_trigger_in_session(db: Any, plan: Any, *, force_once: bool = False) -> AgentTrigger:
    agent = await _load_agent(db, plan.agent_id)
    if agent is None:
        raise HandoffError(f"agent {plan.agent_id} not found for plan {plan.id}")

    trigger = await _ensure_enabled_trigger(db, agent, plan, force_once=force_once)
    return trigger


def _handoff_payload(plan: Any, trigger: AgentTrigger) -> dict[str, Any]:
    payload = {"created_trigger_id": str(trigger.id)}
    logger.info(
        "plan_scheduled_trigger_handoff_completed",
        extra={"plan_id": str(plan.id), **payload},
    )
    return payload


async def _ensure_enabled_trigger(db: Any, agent: Any, plan: Any, *, force_once: bool = False) -> AgentTrigger:
    """Create (or update + re-enable) the plan's wake trigger.

    Idempotent on ``config.plan_id``: re-running for the same plan updates the
    existing trigger in place rather than duplicating. Either way the trigger ends
    enabled with ``config.plan_id`` set.
    """
    payload = _trigger_payload_from_plan(plan, force_once=force_once)

    trigger_result = await db.execute(
        select(AgentTrigger).where(AgentTrigger.agent_id == agent.id).order_by(AgentTrigger.id).with_for_update()
    )
    triggers = list(trigger_result.scalars().all())
    existing = _find_plan_trigger(triggers, plan)

    if existing is not None:
        existing.type = payload["type"]
        existing.config = preserve_trigger_authority(existing, payload["config"])
        existing.reason = payload["reason"]
        existing.is_enabled = True
        await db.flush()
        return existing

    trigger = AgentTrigger(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        name=payload["name"],
        type=payload["type"],
        config=payload["config"],
        reason=payload["reason"],
        is_enabled=True,
    )
    db.add(trigger)
    await db.flush()  # assign trigger.id
    return trigger


def _find_plan_trigger(triggers: list[AgentTrigger], plan: Any) -> AgentTrigger | None:
    plan_id = str(plan.id)
    for trigger in triggers:
        config = getattr(trigger, "config", None) or {}
        if str(config.get("plan_id") or "") == plan_id:
            return trigger
    return None


# ---------------------------------------------------------------------------
# PlanModeService.register_handoff_handler adapter
# ---------------------------------------------------------------------------


async def scheduled_trigger_handoff_handler(db: Any, plan: Any) -> dict[str, Any]:
    """Async handoff handler registered with :class:`PlanModeService`.

    The service passes its live DB session so trigger creation and the plan's
    ``handoff_status`` update commit or roll back together.
    """
    return await handoff_scheduled_trigger(plan, db=db)


def register_scheduled_trigger_handoff(service: Any) -> None:
    """Register :func:`scheduled_trigger_handoff_handler` on a
    :class:`PlanModeService` instance for the ``scheduled_trigger`` target."""
    service.register_handoff_handler(HANDOFF_TARGET, scheduled_trigger_handoff_handler)
