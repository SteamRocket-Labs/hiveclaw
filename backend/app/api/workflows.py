"""Workflow REST API — ephemeral runs and their archive.

Confirmation notes:

* ``POST .../workflows/preview`` — compile + admission + confirmation notes, never runs.
* ``POST .../workflows/runs`` — starts on the user's explicit API call. Optional
  confirmed plan fields are provenance only; they do not force Plan Mode.
* ``GET  .../workflows/runs`` — run history (asset view §4: archived
  definitions + step aggregates + promote provenance).
* ``GET  .../workflows/runs/{run_id}`` — run + step journal.
* ``POST .../workflows/runs/{run_id}/cancel`` — kill (resumable later).
* ``POST .../workflows/runs/{run_id}/promote`` — 固化: archived ephemeral
  definition → registered DRAFT with ``promoted_from_run_id`` provenance
  (activation still walks the human approve-promotion path, §10 decision 4).
* ``GET  .../workflows/promote-suggestions`` — repeated-run evidence.

All endpoints are agent-scoped and gated by :func:`check_agent_access`.
``runtime_tasks.tenant_id`` exists but is nullable/backfilled; the tenant
metadata mirror written at run creation is the authoritative boundary, so
run reads/writes additionally verify the run's ``parent_agent_id`` and that
mirror — an ownership mismatch is indistinguishable from absence (404).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.workflow_definitions import get_workflow_definition_service, record_payload
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.services.workflow_definitions import WorkflowDefinitionError, WorkflowDefinitionService
from app.services.workflow_launch import inspect_workflow_confirmation_needs, start_ephemeral_workflow_for_agent
from app.services.workflow_promote_suggestions import collect_promote_suggestions
from app.services.workflow_runtime_service import LoadedWorkflowRun, WorkflowRuntimeService

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


def _compile_and_assess(payload: WorkflowPreviewRequest | WorkflowStartRequest):
    from app.config import get_settings

    try:
        compiled = compile_workflow(payload.definition)
        admission = admit_workflow(compiled, args=payload.args, limits=AdmissionLimits.from_settings(get_settings()))
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    confirmation = inspect_workflow_confirmation_needs(compiled, args=payload.args)
    return compiled, admission, confirmation


@router.post("/{agent_id}/workflows/preview")
async def preview_workflow_endpoint(
    agent_id: uuid.UUID,
    payload: WorkflowPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    compiled, admission, confirmation = _compile_and_assess(payload)
    return {
        "definition_hash": compiled.definition_hash,
        "confirmation_required": confirmation.requires_confirmation,
        "confirmation_reasons": confirmation.reasons,
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
    compiled, _admission, confirmation = _compile_and_assess(payload)

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
        "confirmation_required": confirmation.requires_confirmation,
        "confirmation_reasons": confirmation.reasons,
    }


async def _load_owned_run(run_id: uuid.UUID, *, agent) -> LoadedWorkflowRun:
    """Load a run and verify it belongs to this agent + tenant.

    The tenant metadata mirror is the authoritative boundary
    (``runtime_tasks.tenant_id`` exists but is nullable/backfilled); an
    ownership mismatch must be indistinguishable from absence, so both
    cases raise 404.
    """
    service = WorkflowRuntimeService()
    loaded = await service.load_run(run_id, tenant_id=agent.tenant_id)
    if loaded is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow run not found")
    metadata = loaded.task.metadata_json or {}
    if loaded.task.parent_agent_id != agent.id or (
        agent.tenant_id is not None and metadata.get("tenant_id") != str(agent.tenant_id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow run not found")
    return loaded


def _run_summary_payload(summary) -> dict:
    task = summary.task
    metadata = task.metadata_json or {}
    definition = metadata.get("definition_json") or {}
    counts = summary.step_counts or {}
    return {
        "run_id": str(task.id),
        "status": task.status,
        "name": definition.get("name") or "unnamed-workflow",
        "description": definition.get("description", ""),
        "definition_source": metadata.get("definition_source"),
        "definition_hash": metadata.get("definition_hash"),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "steps_total": sum(counts.values()),
        "steps_done": counts.get("done", 0),
        "steps_failed": counts.get("failed", 0),
        "promoted_definition_id": str(summary.promoted_definition_id) if summary.promoted_definition_id else None,
    }


@router.get("/{agent_id}/workflows/runs")
async def list_workflow_runs(
    agent_id: uuid.UUID,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    service = WorkflowRuntimeService()
    summaries = await service.list_runs_for_agent(agent.id, tenant_id=agent.tenant_id, limit=min(limit, 200))
    return [_run_summary_payload(summary) for summary in summaries]


@router.get("/{agent_id}/workflows/runs/{run_id}")
async def get_workflow_run(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    loaded = await _load_owned_run(run_id, agent=agent)
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
    agent, _access = await check_agent_access(db, current_user, agent_id)
    await _load_owned_run(run_id, agent=agent)  # 404 unless this agent's run
    service = WorkflowRuntimeService()
    try:
        await service.kill_run(run_id, tenant_id=agent.tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"run_id": str(run_id), "status": "killed"}


@router.post("/{agent_id}/workflows/runs/{run_id}/promote")
async def promote_workflow_run(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    definition_service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> dict:
    """固化: register the run's archived definition as a DRAFT template with
    ``promoted_from_run_id`` provenance. Activation still requires the human
    approve-promotion step (§10 decision 4)."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Promoting a workflow to a template requires agent manage access",
        )
    loaded = await _load_owned_run(run_id, agent=agent)
    if loaded.task.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only completed runs can be promoted to a template",
        )
    definition = (loaded.task.metadata_json or {}).get("definition_json")
    if not definition:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run has no archived definition to promote",
        )
    try:
        record = await definition_service.create_draft(
            tenant_id=agent.tenant_id,
            definition_data=definition,
            created_by_user_id=getattr(current_user, "id", None),
            visibility_scope="agent",
            owner_type="agent",
            owner_id=agent.id,
            promoted_from_run_id=run_id,
        )
    except WorkflowDefinitionError as exc:
        message = str(exc)
        if "not found" in message:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message) from exc
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message) from exc
    return record_payload(record)


@router.get("/{agent_id}/workflows/promote-suggestions")
async def list_promote_suggestions(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    suggestions = await collect_promote_suggestions(tenant_id=agent.tenant_id, agent_id=agent.id)
    return [
        {
            "definition_hash": suggestion.definition_hash,
            "name": suggestion.name,
            "run_count": suggestion.run_count,
            "sample_run_ids": [str(rid) for rid in suggestion.sample_run_ids],
        }
        for suggestion in suggestions
    ]
