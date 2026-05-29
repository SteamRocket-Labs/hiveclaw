"""Objective ledger REST API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.objective import AgentObjective
from app.models.user import User
from app.services import objective_intake, objective_service

router = APIRouter(prefix="/agents", tags=["objectives"])


class ObjectiveOut(BaseModel):
    id: str
    objective_key: str
    description: str
    status: str
    priority: int
    source: str
    success_criteria: str | None = None
    blocked_reason: str | None = None
    metadata: dict[str, Any]
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


class ObjectiveProposalIn(BaseModel):
    description: str = Field(min_length=1)
    objective_key: str | None = None
    success_criteria: str | None = None
    autonomy_class: str = "implicit_inference"
    risk_level: str = "medium"
    confidence: float = 0.7
    priority: int = 0
    wake_policy: dict[str, Any] | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"
    # Plan Mode (§9.3 / §9.4): a confirmed plan authorising autonomous activation.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None


class ObjectiveUpdateIn(BaseModel):
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    success_criteria: str | None = None
    blocked_reason: str | None = None
    completion_evidence: str | None = None
    metadata: dict[str, Any] | None = None
    # Plan Mode (§9.3 / §9.4): a confirmed plan authorising autonomous activation.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None


class ObjectiveDecisionIn(BaseModel):
    reason: str | None = None
    # Plan Mode (§9.3 / §9.4): a confirmed plan authorising autonomous activation.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _objective_out(objective: AgentObjective) -> ObjectiveOut:
    return ObjectiveOut(
        id=str(objective.id),
        objective_key=objective.objective_key,
        description=objective.description,
        status=objective.status,
        priority=objective.priority,
        source=objective.source,
        success_criteria=objective.success_criteria,
        blocked_reason=objective.blocked_reason,
        metadata=objective.metadata_json or {},
        created_at=_dt(objective.created_at),
        updated_at=_dt(objective.updated_at),
        completed_at=_dt(objective.completed_at),
    )


async def _load_agent_objective_or_404(db: AsyncSession, *, agent_id: uuid.UUID, objective_id: uuid.UUID) -> AgentObjective:
    from sqlalchemy import select

    result = await db.execute(
        select(AgentObjective).where(AgentObjective.id == objective_id, AgentObjective.agent_id == agent_id)
    )
    objective = result.scalar_one_or_none()
    if objective is None:
        raise HTTPException(status_code=404, detail="Objective not found")
    return objective


@router.get("/{agent_id}/objectives", response_model=list[ObjectiveOut])
async def list_agent_objectives(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    objectives = await objective_service.list_objectives_for_agent(db, agent_id)
    return [_objective_out(objective) for objective in objectives]


@router.post("/{agent_id}/objectives/proposals", response_model=ObjectiveOut, status_code=status.HTTP_201_CREATED)
async def propose_agent_objective(
    agent_id: uuid.UUID,
    payload: ObjectiveProposalIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access = await check_agent_access(db, current_user, agent_id)
    candidate = objective_intake.ObjectiveCandidate(
        agent_id=agent_id,
        tenant_id=getattr(agent, "tenant_id", None),
        description=payload.description,
        source=payload.source,
        autonomy_class=payload.autonomy_class,
        risk_level=payload.risk_level,
        confidence=payload.confidence,
        evidence=payload.evidence,
        objective_key=payload.objective_key,
        success_criteria=payload.success_criteria,
        priority=payload.priority,
        wake_policy=payload.wake_policy or {},
    )
    # Plan Mode early intercept (§9.3 / §9.4): a proposal that the intake gate would
    # activate immediately opens an autonomous wake and needs a confirmed plan. An
    # inferred objective that stays `proposed` is a preview and is not gated.
    if objective_intake.gate_objective_candidate(candidate).status == "active":
        await enforce_plan_gate(
            db,
            agent_id=agent_id,
            action_kind="activate_objective_wake",
            gate=get_plan_mode_gate(),
            confirmed_plan_id=payload.confirmed_plan_id,
            confirmed_plan_version=payload.confirmed_plan_version,
            confirmed_plan_hash=payload.confirmed_plan_hash,
        )
    objective = await objective_intake.upsert_objective_candidate(db, agent, candidate)
    return _objective_out(objective)


@router.patch("/{agent_id}/objectives/{objective_id}", response_model=ObjectiveOut)
async def update_agent_objective(
    agent_id: uuid.UUID,
    objective_id: uuid.UUID,
    payload: ObjectiveUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    objective = await _load_agent_objective_or_404(db, agent_id=agent_id, objective_id=objective_id)

    # Plan Mode early intercept (§9.3 / §9.4): activating an objective into an
    # autonomous status opens autonomous wake and needs a confirmed plan.
    # Editing other fields (priority, description, completion, blocking) is not gated.
    requested_status_lc = str(payload.status or "").strip().lower()
    activating = (
        requested_status_lc in objective_intake.AUTONOMOUS_OBJECTIVE_STATUSES
        and str(getattr(objective, "status", "") or "").strip().lower() != requested_status_lc
    )
    if activating:
        await enforce_plan_gate(
            db,
            agent_id=agent_id,
            action_kind="activate_objective_wake",
            gate=get_plan_mode_gate(),
            confirmed_plan_id=payload.confirmed_plan_id,
            confirmed_plan_version=payload.confirmed_plan_version,
            confirmed_plan_hash=payload.confirmed_plan_hash,
            action_artifact={"metadata": dict(objective.metadata_json or {})},
        )

    data = payload.model_dump(exclude_unset=True)
    # Plan Mode handoff fields are gate inputs, not ORM columns — drop them before
    # the generic setattr loop below.
    for _plan_field in ("confirmed_plan_id", "confirmed_plan_version", "confirmed_plan_hash"):
        data.pop(_plan_field, None)
    metadata = dict(objective.metadata_json or {})
    metadata_patch = data.pop("metadata", None)
    if isinstance(metadata_patch, dict):
        metadata.update(metadata_patch)
    completion_evidence = str(data.pop("completion_evidence", "") or "").strip()
    requested_status = str(data.get("status") or "").strip().lower()
    if requested_status == "completed":
        completion_evidence = (
            completion_evidence
            or str(metadata.get("completion_evidence") or metadata.get("objective_evidence") or "").strip()
        )
        if not completion_evidence:
            raise HTTPException(
                status_code=400,
                detail="completion_evidence is required when marking an objective completed",
            )
        objective.metadata_json = metadata
    for key, value in data.items():
        if key == "status" and requested_status == "completed":
            continue
        setattr(objective, key, value)
    objective.metadata_json = metadata
    if requested_status == "completed":
        from app.services.objective_evaluator import apply_attempt_evaluation

        apply_attempt_evaluation(
            objective,
            attempted_status="completed",
            evidence=completion_evidence,
            result_summary=str(metadata.get("last_result_summary") or completion_evidence),
        )
    await db.commit()
    return _objective_out(objective)


@router.post("/{agent_id}/objectives/{objective_id}/approve", response_model=ObjectiveOut)
async def approve_agent_objective(
    agent_id: uuid.UUID,
    objective_id: uuid.UUID,
    payload: ObjectiveDecisionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    objective = await _load_agent_objective_or_404(db, agent_id=agent_id, objective_id=objective_id)
    # Plan Mode early intercept (§9.3 / §9.4): approval activates the objective and
    # opens autonomous wake, so it needs a confirmed plan.
    await enforce_plan_gate(
        db,
        agent_id=agent_id,
        action_kind="activate_objective_wake",
        gate=get_plan_mode_gate(),
        confirmed_plan_id=payload.confirmed_plan_id,
        confirmed_plan_version=payload.confirmed_plan_version,
        confirmed_plan_hash=payload.confirmed_plan_hash,
        action_artifact={"metadata": dict(objective.metadata_json or {})},
    )
    from app.services.objective_approval import apply_objective_approval

    apply_objective_approval(
        objective,
        actor_id=getattr(current_user, "id", None),
        decision="approved",
        reason=payload.reason,
    )
    await db.commit()
    return _objective_out(objective)


@router.post("/{agent_id}/objectives/{objective_id}/reject", response_model=ObjectiveOut)
async def reject_agent_objective(
    agent_id: uuid.UUID,
    objective_id: uuid.UUID,
    payload: ObjectiveDecisionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    objective = await _load_agent_objective_or_404(db, agent_id=agent_id, objective_id=objective_id)
    from app.services.objective_approval import apply_objective_approval

    apply_objective_approval(
        objective,
        actor_id=getattr(current_user, "id", None),
        decision="rejected",
        reason=payload.reason,
    )
    await db.commit()
    return _objective_out(objective)
