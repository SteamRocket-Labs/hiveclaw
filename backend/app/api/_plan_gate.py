"""REST confirmation-gate helper.

Thin translation layer between the shared :class:`PlanModeGate` decision service
and a FastAPI route handler. A handler that is about to create or enable an
autonomous artifact calls :func:`enforce_plan_gate` *after* its existing
permission / tenant / validation checks; when the gate blocks the action this
raises ``HTTPException(409)`` carrying a ``requires_confirmation`` envelope.

Keeping the FastAPI import out of :mod:`app.services.plan_mode_gate` (a read-only
service consumed by both the REST and the backstop layers) is deliberate: the
service stays framework-agnostic and this module owns the HTTP translation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.services.plan_mode_gate import get_plan_mode_gate


async def enforce_plan_gate(
    db: Any,
    *,
    agent_id: UUID,
    action_kind: str,
    gate: Any,
    confirmed_plan_id: str | None = None,
    confirmed_plan_version: int | None = None,
    confirmed_plan_hash: str | None = None,
    action_artifact: dict | None = None,
) -> None:
    """Consult the confirmation gate; raise ``409`` when confirmation is required.

    The handler must already have run its access / validation checks and decided
    that this request *opens autonomous behaviour* (an enabled trigger, an
    activated wake objective, a delegation, or an auto-executing task), then
    resolved ``gate`` via the router-local :func:`get_plan_mode_gate` (a single
    patch point per router). On a clean confirmed-plan handoff (or a cutover
    exemption) this returns ``None`` and the handler proceeds; otherwise it
    raises with the gate's confirmation-required payload as the response detail.
    """
    decision = await gate.check(
        db,
        agent_id=agent_id,
        action_kind=action_kind,
        confirmed_plan_id=confirmed_plan_id,
        plan_version=confirmed_plan_version,
        plan_hash=confirmed_plan_hash,
        action_artifact=action_artifact,
    )
    if decision.needs_plan:
        raise HTTPException(status_code=409, detail=decision.needs_plan_payload)


__all__ = ["enforce_plan_gate", "get_plan_mode_gate"]
