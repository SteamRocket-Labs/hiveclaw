"""Workflow REST API — ephemeral runs and their archive.

Confirmation notes:

* ``POST .../workflows/preview`` — compile + admission + confirmation notes, never runs.
* ``POST .../workflows/runs`` — starts on the user's explicit API call. Optional
  confirmed plan fields are provenance only; they do not force Plan Mode.
* ``GET  .../workflows/runs`` — run history (asset view §4: archived
  definitions + step aggregates + promote provenance).
* ``GET  .../workflows/runs/{run_id}`` — run + step journal.
* ``POST .../workflows/runs/{run_id}/cancel`` — kill (resumable later).
* ``POST .../workflows/runs/{run_id}/promotion-proposals`` — the initiating
  session owner submits immutable evidence for independent manager review.
* ``POST .../workflows/promotion-proposals/{id}/review`` — a different human
  manager approves/rejects; approval atomically publishes the Workflow asset.
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
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import authorize_session_action, check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.coordination import CoordinationCheckpoint
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.models.workflow_confirmation import WorkflowPreviewArtifact
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
    WORKFLOW_EXPLICIT_USER_START_SOURCE,
    WorkflowConfirmationConflict,
    WorkflowStartClaim,
    claim_workflow_preview_start,
    create_workflow_preview,
    load_workflow_candidate,
    load_workflow_preview,
    mark_workflow_preview_failed_record,
    mark_workflow_preview_started_record,
    workflow_candidate_preview_id,
    workflow_preview_artifact_payload,
)
from app.services.workflow_promotion_service import (
    WorkflowPromotionConflict,
    WorkflowPromotionForbidden,
    WorkflowPromotionNotFound,
    WorkflowPromotionReviewResult,
    WorkflowPromotionService,
    WorkflowPromotionStaleError,
)
from app.services.workflow_launch import inspect_workflow_confirmation_needs, start_ephemeral_workflow_for_agent
from app.services.workflow_promote_suggestions import collect_promote_suggestions
from app.services.workflow_runtime_service import LoadedWorkflowRun, WorkflowRuntimeService
from app.services.workflow_user_control import queue_workflow_resume_record
from app.services.plan_mode_core import stamp_confirmed_plan_provenance

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
    # Legacy wire compatibility only; PlanModeGate resolves the canonical hash.
    plan_hash: str | None = None
    ledger_todo_id: str | None = None


class WorkflowGateDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=200)
    decision: Literal["approve", "reject"]


class WorkflowPromotionReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=2000)


def get_workflow_promotion_service() -> WorkflowPromotionService:
    return WorkflowPromotionService()


def _promotion_error(exc: Exception) -> None:
    if isinstance(exc, WorkflowPromotionNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, WorkflowPromotionForbidden):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, (WorkflowPromotionConflict, WorkflowPromotionStaleError)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise exc


def _promotion_payload(
    proposal,
    definition,
    *,
    current_user_id: uuid.UUID,
    can_manage: bool,
) -> dict[str, Any]:
    evidence = dict(proposal.run_evidence_json or {})
    definition_json = dict(proposal.definition_json or {})
    return {
        "id": str(proposal.id),
        "run_id": str(proposal.run_id),
        "status": proposal.status,
        "name": definition_json.get("name") or "Workflow",
        "description": definition_json.get("description") or "",
        "requested_by_me": proposal.requester_user_id == current_user_id,
        "can_review": bool(
            can_manage and proposal.status == "pending" and proposal.requester_user_id != current_user_id
        ),
        "can_withdraw": bool(proposal.status == "pending" and proposal.requester_user_id == current_user_id),
        "evidence": {
            "run_status": evidence.get("status"),
            "steps_total": len(evidence.get("steps") or []),
            "leaves_total": len(evidence.get("leaves") or []),
            "completed_at": evidence.get("completed_at"),
        },
        "review_reason": proposal.review_reason,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
        "reviewed_at": proposal.reviewed_at.isoformat() if proposal.reviewed_at else None,
        "definition_id": str(definition.id) if definition is not None else None,
    }


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
            require_writable=True,
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


async def _preview_dynamic_workflow_candidate(
    db: AsyncSession,
    *,
    agent,
    current_user: User,
    proposal_id: uuid.UUID,
    candidate_id: str,
) -> dict[str, Any]:
    """Create/replay the immutable preview for the exact stored candidate."""

    normalized_candidate_id = str(candidate_id or "").strip()
    if not normalized_candidate_id or len(normalized_candidate_id) > 160:
        raise WorkflowConfirmationConflict("candidate_not_found", "Dynamic Workflow candidate_id is invalid.")
    proposal, candidate = await load_workflow_candidate(
        db,
        proposal_id=proposal_id,
        candidate_id=normalized_candidate_id,
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        session_id=None,
        user_id=current_user.id,
        for_update=True,
    )
    await authorize_session_action(
        db,
        current_user,
        agent_id=agent.id,
        session_id=proposal.session_id,
        action="workflow:candidate_preview",
        require_writable=True,
    )
    preview_id = workflow_candidate_preview_id(
        proposal_id=proposal.id,
        candidate_id=normalized_candidate_id,
    )
    existing = (
        await db.execute(
            select(WorkflowPreviewArtifact).where(
                WorkflowPreviewArtifact.id == preview_id,
                WorkflowPreviewArtifact.tenant_id == agent.tenant_id,
                WorkflowPreviewArtifact.agent_id == agent.id,
                WorkflowPreviewArtifact.session_id == proposal.session_id,
                WorkflowPreviewArtifact.requested_by_user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return workflow_preview_artifact_payload(existing)

    try:
        compiled = compile_workflow(dict(candidate.get("lowered_definition") or {}))
        args = normalize_workflow_args(compiled, dict(candidate.get("preview_args") or {}))
        from app.config import get_settings

        admission = admit_workflow(compiled, args=args, limits=AdmissionLimits.from_settings(get_settings()))
        confirmation = inspect_workflow_confirmation_needs(compiled, args=args)
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        raise WorkflowConfirmationConflict("candidate_invalid", str(exc)) from exc
    args_hash = compute_definition_hash(args)
    if compiled.definition_hash != candidate.get("definition_hash") or args_hash != candidate.get("args_hash"):
        raise WorkflowConfirmationConflict(
            "candidate_hash_mismatch",
            "Stored Dynamic Workflow candidate no longer matches its canonical hashes.",
        )
    preview_payload = {
        "ok": True,
        "proposal_id": str(proposal.id),
        "candidate_id": normalized_candidate_id,
        "definition_hash": compiled.definition_hash,
        "args_hash": args_hash,
        "confirmation_required": confirmation.requires_confirmation,
        "confirmation_reasons": confirmation.reasons,
        "planned_leaf_calls": admission.planned_leaf_calls,
        "budget_tokens": admission.budget_tokens,
        "dynamic_candidate": candidate,
        "selection_source": "direct_user_candidate_action",
        "selected_by_user_id": str(current_user.id),
    }
    preview = await create_workflow_preview(
        db,
        preview_id=preview_id,
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        session_id=proposal.session_id,
        user_id=current_user.id,
        definition=compiled.definition.canonical_dict(),
        args=args,
        definition_hash=compiled.definition_hash,
        args_hash=args_hash,
        preview_payload=preview_payload,
        proposal=proposal,
        candidate_id=normalized_candidate_id,
    )
    await db.commit()
    return workflow_preview_artifact_payload(preview)


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


@router.post("/{agent_id}/workflows/proposals/{proposal_id}/candidates/{candidate_id}/preview")
async def preview_dynamic_workflow_candidate_endpoint(
    agent_id: uuid.UUID,
    proposal_id: uuid.UUID,
    candidate_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    try:
        return await _preview_dynamic_workflow_candidate(
            db,
            agent=agent,
            current_user=current_user,
            proposal_id=proposal_id,
            candidate_id=candidate_id,
        )
    except WorkflowConfirmationConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


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
            confirmation_source=WORKFLOW_EXPLICIT_USER_START_SOURCE,
            confirmation_evidence_id=request_id,
        )
    except WorkflowConfirmationConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.message}
        ) from exc

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

    plan_authorization: dict[str, Any] | None = None
    if payload.confirmed_plan_id:
        action_artifact = {
            "preview_id": str(preview.id),
            "definition_hash": preview.definition_hash,
            "args_hash": preview.args_hash,
            "artifact_version": preview.artifact_version,
            "artifact_hash": preview.artifact_hash,
        }
        evidence_id = f"workflow-start:{preview.id}:{request_id}"
        decision = await _plan_gate_check(
            db,
            agent_id=agent_id,
            requester_user_id=current_user.id,
            session_id=str(preview.session_id),
            action_kind="start_workflow",
            target_ref=f"workflow-preview:{preview.id}",
            confirmed_plan_id=payload.confirmed_plan_id,
            plan_version=payload.plan_version,
            plan_hash=payload.plan_hash,
            action_artifact=action_artifact,
            evidence_id=evidence_id,
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=getattr(decision, "needs_plan_payload", None)
                or {"code": getattr(decision, "reason", "plan_authorization_denied")},
            )
        plan_authorization = stamp_confirmed_plan_provenance(
            {},
            plan_id=getattr(decision, "canonical_plan_id", None),
            plan_version=getattr(decision, "canonical_plan_version", None),
            plan_hash=getattr(decision, "canonical_plan_hash", None),
            authorization_lease_id=getattr(decision, "authorization_lease_id", None),
            canonical_args_hash=getattr(decision, "canonical_args_hash", None),
            target_ref=getattr(decision, "target_ref", None),
            requester_user_id=current_user.id,
            session_id=str(preview.session_id),
            evidence_id=evidence_id,
        ).get("plan_authorization")

    # The preview claim and optional PlanAuthorizationLease consumption share
    # this commit. A failed launch can be retried through the preview recovery
    # state with the same immutable evidence id.
    await db.commit()

    claim_token = preview.claim_token
    run_id = preview.run_id
    if claim_token is None or run_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Workflow start claim is incomplete"
        )

    definition = dict(preview.definition_json or {})
    args = dict(preview.args_json or {})
    dynamic_candidate = mapping((preview.preview_json or {}).get("dynamic_candidate"))
    run_metadata = (
        build_dynamic_workflow_run_metadata(
            proposal_id=str(preview.proposal_id) if preview.proposal_id else None,
            candidate_id=preview.candidate_id,
            preview_id=str(preview.id),
            definition_hash=preview.definition_hash,
            args_hash=preview.args_hash,
            candidate=dynamic_candidate,
        )
        or {}
    )
    run_metadata["workflow_confirmation"] = {
        "preview_id": str(preview.id),
        "artifact_version": preview.artifact_version,
        "artifact_hash": preview.artifact_hash,
        "confirmed_by_user_id": str(preview.confirmed_by_user_id),
        "confirmation_source": preview.confirmation_source,
        "confirmation_evidence_id": preview.confirmation_evidence_id,
    }
    if plan_authorization:
        run_metadata["plan_authorization"] = plan_authorization
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


async def _authorize_workflow_run_action(
    db: AsyncSession,
    *,
    agent,
    current_user: User,
    loaded: LoadedWorkflowRun,
    action: str,
):
    """Bind a Workflow action to the initiating user's parent session."""

    parent_session_id = getattr(loaded.task, "parent_session_id", None)
    try:
        session_id = uuid.UUID(str(parent_session_id or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow run has no valid parent session authority",
        ) from exc
    return await authorize_session_action(
        db,
        current_user,
        agent_id=agent.id,
        session_id=session_id,
        action=action,
        require_writable=True,
    )


async def _visible_workflow_summaries(
    db: AsyncSession,
    *,
    agent,
    current_user: User,
    summaries: list[Any],
) -> list[Any]:
    """Keep the run history on the same per-user session authority boundary."""

    result = await db.execute(
        select(ChatSession.id).where(
            ChatSession.agent_id == agent.id,
            ChatSession.user_id == current_user.id,
        )
    )
    owned_session_ids = {str(session_id) for session_id in result.scalars().all()}
    visible: list[Any] = []
    for summary in summaries:
        task = summary.task
        parent_session_id = str(getattr(task, "parent_session_id", None) or "")
        if parent_session_id and parent_session_id in owned_session_ids:
            visible.append(summary)
            continue
        if not parent_session_id:
            metadata_user_id = str((getattr(task, "metadata_json", None) or {}).get("user_id") or "")
            if metadata_user_id and metadata_user_id == str(current_user.id):
                visible.append(summary)
    return visible


async def _queue_workflow_resume(
    db: AsyncSession,
    *,
    loaded: LoadedWorkflowRun,
    agent,
    current_user: User,
    request_kind: Literal["repair", "gate_approved", "gate_rejected"],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = (
        await db.execute(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == loaded.task.id,
                RuntimeTask.task_type == "workflow",
                RuntimeTask.tenant_id == agent.tenant_id,
                RuntimeTask.parent_agent_id == agent.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow run not found")
    try:
        queue_workflow_resume_record(
            task,
            request_kind=request_kind,
            actor_user_id=current_user.id,
            details=details,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    from app.services.runtime_task_worker import notify_runtime_task_worker

    await notify_runtime_task_worker(reason=f"workflow_{request_kind}_queued", runtime_task_id=task.id)
    return {
        "run_id": str(task.id),
        "status": "pending",
        "reason": f"{request_kind}_queued",
    }


async def _apply_workflow_gate_decision(
    db: AsyncSession,
    *,
    loaded: LoadedWorkflowRun,
    agent,
    current_user: User,
    step_id: str,
    decision: Literal["approve", "reject"],
) -> dict[str, Any]:
    checkpoint = (
        await db.execute(
            select(CoordinationCheckpoint)
            .where(
                CoordinationCheckpoint.tenant_id == agent.tenant_id,
                CoordinationCheckpoint.extra_metadata["workflow_run_id"].as_string() == str(loaded.task.id),
                CoordinationCheckpoint.extra_metadata["workflow_step_id"].as_string() == step_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if checkpoint is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="workflow gate checkpoint not found")

    target_status = "approved" if decision == "approve" else "rejected"
    replayed = checkpoint.status == target_status
    if checkpoint.status not in {"pending", target_status}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"workflow gate was already {checkpoint.status}",
        )
    if not replayed:
        checkpoint.status = target_status
        metadata = dict(checkpoint.extra_metadata or {})
        metadata.update(
            {
                "decided_by_user_id": str(current_user.id),
                "decision": decision,
                "decided_at": datetime.now(UTC).isoformat(),
            }
        )
        checkpoint.extra_metadata = metadata
    if loaded.task.status == "suspended":
        queued = await _queue_workflow_resume(
            db,
            loaded=loaded,
            agent=agent,
            current_user=current_user,
            request_kind="gate_approved" if decision == "approve" else "gate_rejected",
            details={
                "step_id": step_id,
                "decision": decision,
                "checkpoint_id": str(checkpoint.id),
            },
        )
        return {**queued, "step_id": step_id, "decision": decision, "replayed": replayed}
    await db.commit()
    return {
        "run_id": str(loaded.task.id),
        "status": loaded.task.status,
        "step_id": step_id,
        "decision": decision,
        "replayed": True,
    }


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
    summaries = await _visible_workflow_summaries(
        db,
        agent=agent,
        current_user=current_user,
        summaries=summaries,
    )
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
    await _authorize_workflow_run_action(
        db,
        agent=agent,
        current_user=current_user,
        loaded=loaded,
        action="workflow:read",
    )
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
    loaded = await _load_owned_run(run_id, agent=agent)  # 404 unless this agent's run
    await _authorize_workflow_run_action(
        db,
        agent=agent,
        current_user=current_user,
        loaded=loaded,
        action="workflow:cancel",
    )
    service = WorkflowRuntimeService()
    try:
        resulting_status = await service.kill_run(run_id, tenant_id=agent.tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"run_id": str(run_id), "status": resulting_status}


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
    await _authorize_workflow_run_action(
        db,
        agent=agent,
        current_user=current_user,
        loaded=loaded,
        action="workflow:repair",
    )
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

    return await _queue_workflow_resume(
        db,
        loaded=loaded,
        agent=agent,
        current_user=current_user,
        request_kind="repair",
    )


@router.post("/{agent_id}/workflows/runs/{run_id}/gate-decision")
async def decide_workflow_gate(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: WorkflowGateDecisionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    loaded = await _load_owned_run(run_id, agent=agent)
    await _authorize_workflow_run_action(
        db,
        agent=agent,
        current_user=current_user,
        loaded=loaded,
        action="workflow:gate_decision",
    )
    return await _apply_workflow_gate_decision(
        db,
        loaded=loaded,
        agent=agent,
        current_user=current_user,
        step_id=payload.step_id,
        decision=payload.decision,
    )


@router.post("/{agent_id}/workflows/runs/{run_id}/promotion-proposals")
async def submit_workflow_promotion_proposal(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    promotion_service: WorkflowPromotionService = Depends(get_workflow_promotion_service),
) -> dict:
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    try:
        proposal = await promotion_service.submit(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            run_id=run_id,
            requester_user_id=current_user.id,
        )
    except (WorkflowPromotionNotFound, WorkflowPromotionForbidden, WorkflowPromotionConflict) as exc:
        _promotion_error(exc)
    return _promotion_payload(
        proposal,
        None,
        current_user_id=current_user.id,
        can_manage=access_level == "manage",
    )


@router.get("/{agent_id}/workflows/promotion-proposals")
async def list_workflow_promotion_proposals(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    promotion_service: WorkflowPromotionService = Depends(get_workflow_promotion_service),
) -> list[dict]:
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    rows = await promotion_service.list_proposals(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        requester_user_id=current_user.id,
        include_all=access_level == "manage",
    )
    return [
        _promotion_payload(
            proposal,
            definition,
            current_user_id=current_user.id,
            can_manage=access_level == "manage",
        )
        for proposal, definition in rows
    ]


@router.post("/{agent_id}/workflows/promotion-proposals/{proposal_id}/review")
async def review_workflow_promotion_proposal(
    agent_id: uuid.UUID,
    proposal_id: uuid.UUID,
    payload: WorkflowPromotionReviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    promotion_service: WorkflowPromotionService = Depends(get_workflow_promotion_service),
) -> dict:
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workflow promotion review requires agent manage access",
        )
    try:
        reviewed: WorkflowPromotionReviewResult = await promotion_service.review(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            proposal_id=proposal_id,
            reviewer_user_id=current_user.id,
            decision=payload.decision,
            reason=payload.reason,
        )
    except (
        WorkflowPromotionNotFound,
        WorkflowPromotionForbidden,
        WorkflowPromotionConflict,
        WorkflowPromotionStaleError,
    ) as exc:
        _promotion_error(exc)
    return _promotion_payload(
        reviewed.proposal,
        reviewed.definition,
        current_user_id=current_user.id,
        can_manage=True,
    )


@router.post("/{agent_id}/workflows/promotion-proposals/{proposal_id}/withdraw")
async def withdraw_workflow_promotion_proposal(
    agent_id: uuid.UUID,
    proposal_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    promotion_service: WorkflowPromotionService = Depends(get_workflow_promotion_service),
) -> dict:
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    try:
        proposal = await promotion_service.withdraw(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            proposal_id=proposal_id,
            requester_user_id=current_user.id,
        )
    except (WorkflowPromotionNotFound, WorkflowPromotionForbidden, WorkflowPromotionConflict) as exc:
        _promotion_error(exc)
    return _promotion_payload(
        proposal,
        None,
        current_user_id=current_user.id,
        can_manage=access_level == "manage",
    )


@router.get("/{agent_id}/workflows/promote-suggestions")
async def list_promote_suggestions(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workflow promotion evidence requires agent manage access",
        )
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
