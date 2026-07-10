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

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.workflow_definitions import get_workflow_definition_service, record_payload
from app.core.permissions import authorize_session_action, check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.user import User
from app.runtime.dynamic_workflow import build_dynamic_workflow_run_metadata, mapping
from app.runtime.workflow_admission import (
    AdmissionLimits,
    WorkflowAdmissionError,
    admit_workflow,
    normalize_workflow_args,
)
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash
from app.services.workflow_confirmation_service import (
    WorkflowConfirmationConflict,
    WorkflowStartClaim,
    claim_workflow_preview_start,
    create_workflow_preview,
    load_workflow_preview,
    mark_workflow_preview_failed_record,
    mark_workflow_preview_started_record,
    workflow_preview_artifact_payload,
)
from app.services.workflow_definitions import WorkflowDefinitionError, WorkflowDefinitionService
from app.services.workflow_launch import (
    build_resumable_workflow_leaf_executor,
    inspect_workflow_confirmation_needs,
    start_ephemeral_workflow_for_agent,
)
from app.services.workflow_promote_suggestions import collect_promote_suggestions
from app.services.workflow_runtime_service import LoadedWorkflowRun, WorkflowRuntimeService

router = APIRouter(prefix="/agents", tags=["workflows"])
logger = logging.getLogger(__name__)


class WorkflowPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    args: dict[str, Any] = Field(default_factory=dict)
    session_id: uuid.UUID | None = None


class WorkflowStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: uuid.UUID
    confirmed_plan_id: str | None = None
    plan_version: int | None = None
    plan_hash: str | None = None
    ledger_todo_id: str | None = None


async def _plan_gate_check(db: AsyncSession, **kwargs: Any):
    """Seam for the PlanModeGate call (overridable in tests)."""
    from app.services.plan_mode_gate import get_plan_mode_gate

    return await get_plan_mode_gate().check(db, **kwargs)


def _compile_and_assess(payload: WorkflowPreviewRequest):
    from app.config import get_settings

    try:
        compiled = compile_workflow(payload.definition)
        args = normalize_workflow_args(compiled, payload.args)
        admission = admit_workflow(compiled, args=args, limits=AdmissionLimits.from_settings(get_settings()))
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    confirmation = inspect_workflow_confirmation_needs(compiled, args=args)
    return compiled, admission, confirmation, args


async def _resolve_workflow_preview_session(
    db: AsyncSession,
    *,
    agent,
    current_user: User,
    requested_session_id: uuid.UUID | None,
    workflow_name: str,
) -> ChatSession:
    if requested_session_id is not None:
        authority = await authorize_session_action(
            db,
            current_user,
            agent_id=agent.id,
            session_id=requested_session_id,
            action="workflow:preview",
        )
        return authority.session
    session = ChatSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        user_id=current_user.id,
        title=f"Workflow: {workflow_name}"[:200],
        source_channel="web",
        session_kind="workflow_control",
        actor_type="user",
        runtime_source="workflow_preview",
        visibility_scope="direct_user",
        listed_surface="workflow",
    )
    db.add(session)
    await db.flush()
    return session


async def _create_workflow_preview_artifact(db: AsyncSession, **kwargs):
    return await create_workflow_preview(db, **kwargs)


async def _claim_workflow_preview_artifact(db: AsyncSession, **kwargs) -> WorkflowStartClaim:
    return await claim_workflow_preview_start(db, **kwargs)


async def _finish_workflow_preview_artifact(
    db: AsyncSession,
    *,
    preview_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
    claim_token: uuid.UUID,
) -> None:
    preview = await load_workflow_preview(
        db,
        preview_id=preview_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=None,
        user_id=user_id,
        for_update=True,
    )
    mark_workflow_preview_started_record(preview, run_id=run_id, claim_token=claim_token)
    await db.flush()


async def _fail_workflow_preview_artifact(
    db: AsyncSession,
    *,
    preview_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    claim_token: uuid.UUID,
    code: str,
    message: str,
) -> None:
    preview = await load_workflow_preview(
        db,
        preview_id=preview_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=None,
        user_id=user_id,
        for_update=True,
    )
    mark_workflow_preview_failed_record(
        preview,
        claim_token=claim_token,
        code=code,
        message=message,
    )
    await db.flush()


@router.post("/{agent_id}/workflows/preview")
async def preview_workflow_endpoint(
    agent_id: uuid.UUID,
    payload: WorkflowPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    compiled, admission, confirmation, args = _compile_and_assess(payload)
    args_hash = compute_definition_hash(args)
    session = await _resolve_workflow_preview_session(
        db,
        agent=agent,
        current_user=current_user,
        requested_session_id=payload.session_id,
        workflow_name=compiled.definition.name,
    )
    preview_payload = {
        "ok": True,
        "definition_hash": compiled.definition_hash,
        "args_hash": args_hash,
        "confirmation_required": confirmation.requires_confirmation,
        "confirmation_reasons": confirmation.reasons,
        "planned_leaf_calls": admission.planned_leaf_calls,
        "budget_tokens": admission.budget_tokens,
    }
    preview = await _create_workflow_preview_artifact(
        db,
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        session_id=session.id,
        user_id=current_user.id,
        definition=compiled.definition.canonical_dict(),
        args=args,
        definition_hash=compiled.definition_hash,
        args_hash=args_hash,
        preview_payload=preview_payload,
    )
    return {**workflow_preview_artifact_payload(preview), "session_id": str(session.id)}


@router.get("/{agent_id}/workflows/previews/{preview_id}")
async def get_workflow_preview_endpoint(
    agent_id: uuid.UUID,
    preview_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    try:
        preview = await load_workflow_preview(
            db,
            preview_id=preview_id,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            session_id=None,
            user_id=current_user.id,
        )
    except WorkflowConfirmationConflict as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    return {**workflow_preview_artifact_payload(preview), "session_id": str(preview.session_id)}


@router.post("/{agent_id}/workflows/runs")
async def start_workflow_endpoint(
    agent_id: uuid.UUID,
    payload: WorkflowStartRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    request_id = str(getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID") or uuid.uuid4())
    try:
        claim = await _claim_workflow_preview_artifact(
            db,
            preview_id=payload.preview_id,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            session_id=None,
            user_id=current_user.id,
            confirmation_source="api_explicit_start",
            confirmation_evidence_id=request_id,
        )
        await db.commit()
    except WorkflowConfirmationConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.message}) from exc

    preview = claim.preview
    if claim.outcome == "replay":
        return {
            "run_id": str(preview.run_id),
            "status": "replayed",
            "reason": "workflow_preview_already_started",
            "preview_id": str(preview.id),
            "confirmation_required": bool((preview.preview_json or {}).get("confirmation_required")),
            "confirmation_reasons": list((preview.preview_json or {}).get("confirmation_reasons") or []),
        }

    claim_token = preview.claim_token
    run_id = preview.run_id
    if claim_token is None or run_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Workflow start claim is incomplete")

    definition = dict(preview.definition_json or {})
    args = dict(preview.args_json or {})
    dynamic_candidate = mapping((preview.preview_json or {}).get("dynamic_candidate"))
    run_metadata = build_dynamic_workflow_run_metadata(
        proposal_id=str(preview.proposal_id) if preview.proposal_id else None,
        candidate_id=preview.candidate_id,
        preview_id=str(preview.id),
        definition_hash=preview.definition_hash,
        args_hash=preview.args_hash,
        candidate=dynamic_candidate,
    ) or {}
    run_metadata["workflow_confirmation"] = {
        "preview_id": str(preview.id),
        "artifact_version": preview.artifact_version,
        "artifact_hash": preview.artifact_hash,
        "confirmed_by_user_id": str(preview.confirmed_by_user_id),
        "confirmation_source": preview.confirmation_source,
        "confirmation_evidence_id": preview.confirmation_evidence_id,
    }
    try:
        handle = await start_ephemeral_workflow_for_agent(
            agent_id=agent_id,
            definition=definition,
            args=args,
            user_id=current_user.id,
            confirmed_plan_id=payload.confirmed_plan_id,
            ledger_todo_id=payload.ledger_todo_id,
            parent_session_id=preview.session_id,
            root_session_id=preview.session_id,
            definition_source="dynamic_workflow" if preview.proposal_id or preview.candidate_id else "ephemeral",
            run_metadata=run_metadata,
            run_id=run_id,
            enqueue_only=True,
        )
    except Exception as exc:
        logger.exception("Workflow API launch failed for durable preview %s", preview.id)
        try:
            await _fail_workflow_preview_artifact(
                db,
                preview_id=preview.id,
                tenant_id=agent.tenant_id,
                agent_id=agent_id,
                user_id=current_user.id,
                claim_token=claim_token,
                code="workflow_launch_failed",
                message=str(exc),
            )
            await db.commit()
        except Exception:
            logger.exception("Could not persist Workflow API launch failure for preview %s", preview.id)
        if isinstance(exc, LookupError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    reconciliation_pending = False
    try:
        await _finish_workflow_preview_artifact(
            db,
            preview_id=preview.id,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            user_id=current_user.id,
            run_id=handle.run_id,
            claim_token=claim_token,
        )
        await db.commit()
    except Exception:
        reconciliation_pending = True
        logger.exception("Workflow run %s started but API preview finalization failed", handle.run_id)

    return {
        "run_id": str(handle.run_id),
        "status": handle.outcome.status,
        "reason": handle.outcome.reason,
        "preview_id": str(preview.id),
        "definition_hash": preview.definition_hash,
        "confirmation_required": bool((preview.preview_json or {}).get("confirmation_required")),
        "confirmation_reasons": list((preview.preview_json or {}).get("confirmation_reasons") or []),
        "reconciliation_pending": reconciliation_pending,
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
    dynamic = metadata.get("dynamic_workflow") or None
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
        "dynamic_workflow": dynamic,
        "outcome_evidence": (dynamic or {}).get("outcome_evidence") if isinstance(dynamic, dict) else None,
        "repair_plan": (dynamic or {}).get("repair_plan") if isinstance(dynamic, dict) else None,
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
    dynamic = metadata.get("dynamic_workflow") or None
    return {
        "run_id": str(loaded.task.id),
        "status": loaded.task.status,
        "definition_hash": metadata.get("definition_hash"),
        "definition_source": metadata.get("definition_source"),
        "dynamic_workflow": dynamic,
        "outcome_evidence": (dynamic or {}).get("outcome_evidence") if isinstance(dynamic, dict) else None,
        "repair_plan": (dynamic or {}).get("repair_plan") if isinstance(dynamic, dict) else None,
        "steps": [
            {
                "step_id": step.step_id,
                "step_type": step.step_type,
                "status": step.status,
                "error": step.error,
            }
            for step in loaded.steps
        ],
        "leaf_calls": [
            {
                "step_id": leaf.step_id,
                "leaf_id": leaf.leaf_id,
                "status": leaf.status,
                "error": leaf.error,
                "token_usage": leaf.token_usage,
            }
            for leaf in getattr(loaded, "leaf_calls", [])
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


@router.post("/{agent_id}/workflows/runs/{run_id}/repair")
async def repair_workflow_run(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Repair/retry a failed or suspended run by resuming the same journal.

    This does not start a new workflow. The engine replays completed
    step/leaf journal rows and only executes missing/failed work.
    """
    agent, _access = await check_agent_access(db, current_user, agent_id)
    loaded = await _load_owned_run(run_id, agent=agent)
    if loaded.task.status not in {"failed", "suspended", "killed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only failed, suspended, or killed workflow runs can be repaired",
        )
    metadata = loaded.task.metadata_json or {}
    dynamic = metadata.get("dynamic_workflow") if isinstance(metadata, dict) else None
    repair_plan = (dynamic or {}).get("repair_plan") if isinstance(dynamic, dict) else None
    if isinstance(repair_plan, dict) and not repair_plan.get("repairable", False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="dynamic workflow repair plan is not repairable",
        )

    service = WorkflowRuntimeService()
    await service.record_dynamic_repair_attempt(run_id, tenant_id=agent.tenant_id)
    try:
        outcome = await service.resume_run(
            run_id,
            tenant_id=agent.tenant_id,
            leaf_executor=build_resumable_workflow_leaf_executor(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"run_id": str(run_id), "status": outcome.status, "reason": outcome.reason}


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
            "quality_evidence": getattr(suggestion, "quality_evidence", None),
        }
        for suggestion in suggestions
    ]
