"""Workflow REST API (§9 P4) — preview / start / inspect / cancel ephemeral runs.

Risk-graded confirmation (§10 decision 3):

* ``POST .../workflows/preview`` — compile + admission + risk grade, never runs.
* ``POST .../workflows/runs`` — LOW risk starts on the user's explicit call
  (the preview→confirm click IS the in-conversation confirmation); HIGH risk
  requires a confirmed plan (``confirmed_plan_id`` + version/hash) arbitrated
  by PlanModeGate — fail-closed 409 without one.
* ``GET  .../workflows/runs/{run_id}`` — run + step journal.
* ``POST .../workflows/runs/{run_id}/cancel`` — kill (resumable later).

All endpoints are agent-scoped and gated by :func:`check_agent_access`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash
from app.services.workflow_launch import classify_workflow_risk, start_ephemeral_workflow_for_agent
from app.services.workflow_runtime_service import WorkflowRuntimeService

router = APIRouter(prefix="/agents", tags=["workflows"])


class WorkflowPreviewRequest(BaseModel):
    definition: dict[str, Any]
    args: dict[str, Any] = Field(default_factory=dict)


class WorkflowStartRequest(BaseModel):
    definition: dict[str, Any]
    args: dict[str, Any] = Field(default_factory=dict)
    confirmed_plan_id: str | None = None
    plan_version: int | None = None
    plan_hash: str | None = None
    ledger_todo_id: str | None = None


async def _plan_gate_check(db: AsyncSession, **kwargs: Any):
    """Seam for the PlanModeGate call (overridable in tests)."""
    from app.services.plan_mode_gate import get_plan_mode_gate

    return await get_plan_mode_gate().check(db, **kwargs)


def _compile_and_grade(payload: WorkflowPreviewRequest | WorkflowStartRequest):
    from app.config import get_settings

    try:
        compiled = compile_workflow(payload.definition)
        admission = admit_workflow(compiled, args=payload.args, limits=AdmissionLimits.from_settings(get_settings()))
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    risk = classify_workflow_risk(compiled, args=payload.args)
    return compiled, admission, risk


@router.post("/{agent_id}/workflows/preview")
async def preview_workflow_endpoint(
    agent_id: uuid.UUID,
    payload: WorkflowPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    compiled, admission, risk = _compile_and_grade(payload)
    return {
        "definition_hash": compiled.definition_hash,
        "risk": risk.level,
        "risk_reasons": risk.reasons,
        "planned_leaf_calls": admission.planned_leaf_calls,
        "budget_tokens": admission.budget_tokens,
    }


@router.post("/{agent_id}/workflows/runs")
async def start_workflow_endpoint(
    agent_id: uuid.UUID,
    payload: WorkflowStartRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    compiled, _admission, risk = _compile_and_grade(payload)

    if risk.level == "high":
        decision = await _plan_gate_check(
            db,
            agent_id=agent_id,
            action_kind="start_workflow",
            confirmed_plan_id=payload.confirmed_plan_id,
            plan_version=payload.plan_version,
            plan_hash=payload.plan_hash,
            action_artifact={
                "definition_hash": compiled.definition_hash,
                "args_hash": compute_definition_hash(payload.args),
                "risk_reasons": risk.reasons,
            },
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "plan_required",
                    "reason": decision.reason or "high-risk workflow requires a confirmed plan",
                    "risk_reasons": risk.reasons,
                },
            )

    try:
        handle = await start_ephemeral_workflow_for_agent(
            agent_id=agent_id,
            definition=payload.definition,
            args=payload.args,
            user_id=getattr(current_user, "id", None),
            confirmed_plan_id=payload.confirmed_plan_id,
            ledger_todo_id=payload.ledger_todo_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return {
        "run_id": str(handle.run_id),
        "status": handle.outcome.status,
        "reason": handle.outcome.reason,
        "definition_hash": compiled.definition_hash,
        "risk": risk.level,
    }


@router.get("/{agent_id}/workflows/runs/{run_id}")
async def get_workflow_run(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent = await check_agent_access(db, current_user, agent_id)
    service = WorkflowRuntimeService()
    loaded = await service.load_run(run_id, tenant_id=getattr(agent, "tenant_id", None))
    if loaded is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow run not found")
    metadata = loaded.task.metadata_json or {}
    return {
        "run_id": str(loaded.task.id),
        "status": loaded.task.status,
        "definition_hash": metadata.get("definition_hash"),
        "definition_source": metadata.get("definition_source"),
        "steps": [
            {
                "step_id": step.step_id,
                "step_type": step.step_type,
                "status": step.status,
                "error": step.error,
            }
            for step in loaded.steps
        ],
    }


@router.post("/{agent_id}/workflows/runs/{run_id}/cancel")
async def cancel_workflow_run(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent = await check_agent_access(db, current_user, agent_id)
    service = WorkflowRuntimeService()
    try:
        await service.kill_run(run_id, tenant_id=getattr(agent, "tenant_id", None))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"run_id": str(run_id), "status": "killed"}
