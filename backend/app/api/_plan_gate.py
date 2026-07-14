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
from app.services.plan_mode_core import stamp_confirmed_plan_provenance


async def enforce_plan_gate(
    db: Any,
    *,
    agent_id: UUID,
    requester_user_id: UUID | str | None = None,
    session_id: str | None = None,
    runtime_task_id: UUID | str | None = None,
    action_kind: str,
    target_ref: str | None = None,
    gate: Any,
    confirmed_plan_id: str | None = None,
    confirmed_plan_version: int | None = None,
    confirmed_plan_hash: str | None = None,
    action_artifact: dict | None = None,
    evidence_id: str | None = None,
) -> Any:
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
        requester_user_id=requester_user_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        action_kind=action_kind,
        target_ref=target_ref,
        confirmed_plan_id=confirmed_plan_id,
        plan_version=confirmed_plan_version,
        plan_hash=confirmed_plan_hash,
        action_artifact=action_artifact,
        evidence_id=evidence_id,
    )
    if decision.needs_plan:
        raise HTTPException(status_code=409, detail=decision.needs_plan_payload)
    return decision


def stamp_plan_gate_decision(
    artifact: dict | None,
    *,
    decision: Any,
    confirmed_plan_id: Any,
    confirmed_plan_version: Any,
    confirmed_plan_hash: Any,
    requester_user_id: Any,
    session_id: Any = None,
    runtime_task_id: Any = None,
    evidence_id: Any = None,
) -> dict:
    """Persist the consumed lease proof and server-canonical plan binding."""

    plan_id = getattr(decision, "canonical_plan_id", None) or confirmed_plan_id
    plan_version = getattr(decision, "canonical_plan_version", None)
    if plan_version is None:
        plan_version = confirmed_plan_version
    # Hash authority never falls back to a request-body echo. A successful
    # confirmed-plan decision always carries the server-canonical hash.
    plan_hash = getattr(decision, "canonical_plan_hash", None)

    return stamp_confirmed_plan_provenance(
        artifact,
        plan_id=plan_id,
        plan_version=plan_version,
        plan_hash=plan_hash,
        authorization_lease_id=getattr(decision, "authorization_lease_id", None),
        canonical_args_hash=getattr(decision, "canonical_args_hash", None),
        target_ref=getattr(decision, "target_ref", None),
        requester_user_id=requester_user_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        evidence_id=evidence_id,
    )


__all__ = ["enforce_plan_gate", "get_plan_mode_gate", "stamp_plan_gate_decision"]
