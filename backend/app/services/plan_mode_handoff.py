"""Plan Mode ``objective_trigger`` handoff — confirmed plan -> objective + wake.

This is the first concrete handoff target (``docs/plan-mode-design.md`` §13 /
Phase 4). A *confirmed* ``objective_trigger`` plan is the only thing that may
create an enabled autonomous wake policy, and this module is where that
creation happens. It plugs into :class:`PlanModeService` via
:func:`register_objective_trigger_handoff` — the service awaits
:func:`objective_trigger_handoff_handler` after a successful confirmation.

Design:

* **Reuse, don't reinvent.** Objective resolution goes through the existing
  :func:`ensure_objective_for_trigger`; wake-policy -> trigger translation goes
  through :func:`build_objective_trigger_payload`. This module only adds the
  Plan-Mode-specific provenance (objective ``metadata.plan_id`` and, critically,
  trigger ``config.plan_id``) on top of those helpers.
* **The ``config.plan_id`` contract is load-bearing.** The trigger-daemon
  backstop (task #4) treats an autonomous trigger as legitimate iff it can prove
  it came from a confirmed plan. The proof is ``config.plan_id`` pointing at a
  ``confirmed`` :class:`AgentPlanRequest`. Without it the backstop would
  quarantine the trigger this handoff just created.
* **Atomic + idempotent.** Objective + trigger are written in a single
  transaction owned by :class:`PlanModeService`. Re-running for the same plan
  updates the existing trigger instead of creating a duplicate (handoff
  idempotency, §13).
* **Fail loud.** Invalid plan state or a missing agent raises
  :class:`HandoffError`; :class:`PlanModeService` catches it and records
  ``handoff_status="failed"`` without mutating ``status`` (§13). No partial
  silent success.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.objective import AgentObjective
from app.models.trigger import AgentTrigger
from app.services.focus_state import normalize_focus_task_id
from app.services.objective_service import ensure_objective_for_trigger
from app.services.objective_wake_reconciler import (
    _existing_trigger_for_name,
    build_objective_trigger_payload,
    objective_has_enabled_wake,
)

logger = logging.getLogger(__name__)

HANDOFF_TARGET = "objective_trigger"


class HandoffError(Exception):
    """A handoff that could not be completed; surfaced so the caller records
    ``handoff_status="failed"`` rather than treating it as success."""


async def _load_agent(db: Any, agent_id: uuid.UUID | str) -> Agent | None:
    """Load the owning agent (split out so tests can stub it)."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


def _plan_provenance(plan: Any) -> dict[str, Any]:
    return {
        "plan_id": str(plan.id),
        "plan_version": plan.plan_version,
        "plan_hash": plan.plan_hash,
    }


def _objective_description(plan: Any) -> str:
    plan_json = plan.plan_json or {}
    return str(plan_json.get("objective") or plan_json.get("title") or plan.original_request or "Planned objective")


def _plan_focus_ref(plan: Any) -> str:
    """Stable focus key for a plan-born objective.

    A plan-driven objective has no focus.md checklist ref and no pre-existing
    objective id, so :func:`ensure_objective_for_trigger` would otherwise refuse
    to create one. We derive a deterministic key from the plan title (falling
    back to the plan id) so the objective + its trigger are consistently keyed
    and the handoff is idempotent across re-runs.
    """
    plan_json = plan.plan_json or {}
    basis = str(plan_json.get("title") or "").strip()
    key = normalize_focus_task_id(basis) if basis else ""
    if key in ("", "task"):
        key = normalize_focus_task_id(f"plan_{plan.id}")
    return key


def _objective_for_wake_payload(objective: AgentObjective, plan: Any) -> AgentObjective:
    """Project the wake_policy from the plan onto the objective metadata so the
    shared :func:`build_objective_trigger_payload` produces the right trigger
    type/config. We do not mutate the persisted objective beyond stamping
    provenance + wake_policy; ``build_objective_trigger_payload`` only reads."""
    plan_json = plan.plan_json or {}
    wake_policy = dict(plan_json.get("wake_policy") or {})
    metadata = dict(objective.metadata_json or {})
    metadata.update(_plan_provenance(plan))
    if wake_policy:
        metadata["wake_policy"] = wake_policy
    objective.metadata_json = metadata
    return objective


async def handoff_objective_trigger(plan: Any, *, db: Any | None = None) -> dict[str, Any]:
    """Create/activate the objective + enabled trigger for a confirmed plan.

    Returns the audit payload recorded on ``handoff_payload`` (created ids).

    Raises:
        HandoffError: if the plan is not ``confirmed`` or its agent is missing.
    """
    if getattr(plan, "status", None) != "confirmed":
        raise HandoffError(
            f"objective_trigger handoff requires a confirmed plan (status={getattr(plan, 'status', None)!r})"
        )

    if db is not None:
        objective, trigger = await _handoff_objective_trigger_in_session(db, plan)
        return _handoff_payload(plan, objective, trigger)

    async with async_session() as owned_db:
        try:
            objective, trigger = await _handoff_objective_trigger_in_session(owned_db, plan)
            await owned_db.commit()
        except HandoffError:
            await owned_db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as typed error, not swallowed
            await owned_db.rollback()
            logger.warning(
                "plan_objective_trigger_handoff_failed",
                extra={"plan_id": str(plan.id), "error": str(exc)},
            )
            raise HandoffError(f"objective_trigger handoff failed: {exc}") from exc
        return _handoff_payload(plan, objective, trigger)


async def _handoff_objective_trigger_in_session(db: Any, plan: Any) -> tuple[AgentObjective, AgentTrigger]:
    agent = await _load_agent(db, plan.agent_id)
    if agent is None:
        raise HandoffError(f"agent {plan.agent_id} not found for plan {plan.id}")

    objective = await ensure_objective_for_trigger(
        db,
        agent,
        focus_ref=_plan_focus_ref(plan),
        description=_objective_description(plan),
        objective_id=None,
    )
    if objective is None:
        raise HandoffError(f"could not resolve an objective for plan {plan.id}")

    # Stamp provenance + wake_policy and promote to active.
    _objective_for_wake_payload(objective, plan)
    objective.status = "active"
    objective.blocked_reason = None
    await db.flush()  # ensure objective.id is assigned

    trigger = await _ensure_enabled_trigger(db, agent, objective, plan)
    return objective, trigger


def _handoff_payload(plan: Any, objective: AgentObjective, trigger: AgentTrigger) -> dict[str, Any]:
    payload = {
        "created_objective_id": str(objective.id),
        "created_trigger_id": str(trigger.id),
    }
    logger.info(
        "plan_objective_trigger_handoff_completed",
        extra={"plan_id": str(plan.id), **payload},
    )
    return payload


async def _ensure_enabled_trigger(db: Any, agent: Any, objective: AgentObjective, plan: Any) -> AgentTrigger:
    """Create (or update + re-enable) the objective's wake trigger.

    Idempotent: if an enabled wake already exists for this objective, or a
    trigger with the canonical name exists, it is updated in place rather than
    duplicated. Either way the trigger ends enabled with ``config.plan_id`` set.
    """
    payload = build_objective_trigger_payload(objective)
    config = dict(payload["config"])
    config["plan_id"] = str(plan.id)  # load-bearing backstop contract
    config["plan_version"] = plan.plan_version
    config["plan_hash"] = plan.plan_hash

    trigger_result = await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent.id))
    triggers = list(trigger_result.scalars().all())

    existing = _existing_trigger_for_name(triggers, agent_id=agent.id, name=payload["name"])
    if existing is None and objective_has_enabled_wake(objective, triggers):
        # An enabled wake already exists under a different name; find and reuse it.
        existing = _find_objective_trigger(triggers, objective)

    if existing is not None:
        existing.type = payload["type"]
        existing.config = config
        existing.reason = payload["reason"]
        existing.focus_ref = payload["focus_ref"]
        existing.is_enabled = True
        await db.flush()
        return existing

    trigger = AgentTrigger(
        agent_id=agent.id,
        name=payload["name"],
        type=payload["type"],
        config=config,
        reason=payload["reason"],
        focus_ref=payload["focus_ref"],
        is_enabled=True,
    )
    db.add(trigger)
    await db.flush()  # assign trigger.id
    return trigger


def _find_objective_trigger(triggers: list[AgentTrigger], objective: AgentObjective) -> AgentTrigger | None:
    objective_id = str(objective.id)
    for trigger in triggers:
        config = getattr(trigger, "config", None) or {}
        if str(config.get("objective_id") or "") == objective_id:
            return trigger
    return None


# ---------------------------------------------------------------------------
# PlanModeService.register_handoff_handler adapter
# ---------------------------------------------------------------------------


async def objective_trigger_handoff_handler(db: Any, plan: Any) -> dict[str, Any]:
    """Async handoff handler registered with :class:`PlanModeService`.

    The service passes its live DB session so objective/trigger creation and
    the plan's ``handoff_status`` update commit or roll back together.
    """
    return await handoff_objective_trigger(plan, db=db)


def register_objective_trigger_handoff(service: Any) -> None:
    """Register :func:`objective_trigger_handoff_handler` on a
    :class:`PlanModeService` instance for the ``objective_trigger`` target."""
    service.register_handoff_handler(HANDOFF_TARGET, objective_trigger_handoff_handler)
