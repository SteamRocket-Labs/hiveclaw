"""Plan Mode REST API — the user-facing surface of the plan ledger (§11).

Endpoints (all agent-scoped, all gated by :func:`check_agent_access`)::

    POST   /agents/{agent_id}/plans                     create + generate
    GET    /agents/{agent_id}/plans                      list
    GET    /agents/{agent_id}/plans/{plan_id}            fetch one
    POST   /agents/{agent_id}/plans/{plan_id}/revise     supersede + regenerate
    POST   /agents/{agent_id}/plans/{plan_id}/confirm    confirm (version+hash bound)
    POST   /agents/{agent_id}/plans/{plan_id}/reject     reject
    POST   /agents/{agent_id}/plans/{plan_id}/handoff    hand off to execution

The heavy lifting lives in :class:`PlanModeService`; this module is a thin HTTP
shell that maps the service's typed errors onto status codes:

* :class:`PermissionError`     -> 403 (missing authenticated confirmer, §8.5)
* :class:`PlanConflictError`   -> 409 (wrong status / version / hash, §8.2)
* :class:`ValueError`          -> 400 (bad intent_type)
* plan not found / not this agent -> 404 (tenant + agent isolation)

The confirm body binds to ``plan_version`` + ``plan_hash`` so the user confirms
a specific immutable plan version, never a mutable chat blob (§8.2).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.plan_recommendation import AgentPlanRecommendation
from app.models.plan_request import AgentPlanRequest
from app.models.user import User
from app.services.plan_mode_recommendation_service import create_plan_recommendation, decline_recommendation
from app.services.plan_mode_service import PlanConflictError, PlanModeService
from app.services.plan_mode_service import get_plan_mode_service as _get_shared_plan_mode_service
from app.services.plan_verification_service import verify_plan_artifact

router = APIRouter(prefix="/agents", tags=["plans"])

#: Bound to the *shared* service-layer singleton so the REST API, the tool gate,
#: and the chat/Feishu auto-sync paths all operate on one instance — the one
#: handoff handlers register onto at startup (see
#: ``app.services.plan_mode_registry.register_plan_mode_handoffs``). Kept as a
#: module attribute so tests can monkeypatch this single seam.
_service = _get_shared_plan_mode_service()


def get_plan_mode_service() -> PlanModeService:
    """Return the shared :class:`PlanModeService` (the one handlers register on)."""
    return _service


async def _author_draft_plan(
    service: PlanModeService,
    plan: AgentPlanRequest,
    *,
    fill: dict[str, Any] | None,
    plan_id: uuid.UUID | None = None,
) -> AgentPlanRequest:
    """Author a freshly created/reset draft ``plan``'s plan_json (cut ③/④).

    Launches a system_plan_run so the agent authors the plan in its own main loop
    and ``exit_plan_mode`` fills THIS draft (stable id); then re-loads the row to
    return the authored result. This is the single plan-authoring path — the
    isolated RPC planner was removed in cut ④. Fail-closed: a plan that fails
    authoring is returned in its non-confirmable state (the launcher never
    executes the planned work).

    ``plan_id`` defaults to ``plan.id`` but may be passed explicitly (regenerate
    keys the re-load on the URL plan id).
    """
    from app.services.plan_mode_system_run import launch_system_plan_run

    target_id = plan_id or plan.id
    await launch_system_plan_run(plan, seed_context=fill or None)
    reloaded = await service.get_plan(target_id)
    return reloaded or plan


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PlanCreateIn(BaseModel):
    original_request: str = Field(min_length=1)
    intent_type: str
    source: str = "web_chat"
    session_id: str | None = None
    fill: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class PlanReviseIn(BaseModel):
    fill: dict[str, Any] | None = None


class PlanConfirmIn(BaseModel):
    plan_version: int
    plan_hash: str
    reason: str | None = None


class PlanDecisionIn(BaseModel):
    reason: str | None = None


class PlanVerifyIn(BaseModel):
    evidence_refs: list[str] = Field(default_factory=list)
    completed_criteria: list[str] = Field(default_factory=list)
    failed_criteria: list[str] = Field(default_factory=list)
    notes: str = ""


class PlanRecommendationCreateIn(BaseModel):
    original_request: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    source: str = "web_chat"
    title: str | None = None
    intent_type: str = "autonomous_wake"
    action_kind: str = "create_enabled_trigger"
    tool_name: str = "set_trigger"
    metadata: dict[str, Any] | None = None


class PlanOut(BaseModel):
    id: str
    agent_id: str
    tenant_id: str | None = None
    session_id: str | None = None
    runtime_task_id: str | None = None
    requested_by_user_id: str | None = None
    source: str
    intent_type: str
    original_request: str
    status: str
    plan_version: int
    plan_hash: str | None = None
    plan_markdown_path: str | None = None
    plan_json: dict[str, Any]
    handoff_status: str | None = None
    handoff_payload: dict[str, Any] | None = None
    confirmed_by_user_id: str | None = None
    confirmed_at: str | None = None
    rejected_by_user_id: str | None = None
    rejected_at: str | None = None
    superseded_by_plan_id: str | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any]


class PlanConfirmOut(BaseModel):
    ok: bool
    status: str
    plan_id: str
    handoff_status: str | None = None


class PlanHandoffOut(BaseModel):
    ok: bool
    status: str
    plan_id: str
    handoff_status: str | None = None
    handoff_payload: dict[str, Any] | None = None


class PlanVerificationOut(BaseModel):
    ok: bool
    plan_id: str
    verification: dict[str, Any]


class PlanRecommendationOut(BaseModel):
    id: str
    agent_id: str
    tenant_id: str | None = None
    session_id: str
    runtime_task_id: str | None = None
    recommended_to_user_id: str
    source: str
    intent_type: str
    action_kind: str
    tool_name: str
    title: str
    original_request: str
    status: str
    declined_by_user_id: str | None = None
    declined_at: str | None = None
    accepted_by_user_id: str | None = None
    accepted_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _sid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _plan_out(plan: AgentPlanRequest) -> PlanOut:
    return PlanOut(
        id=str(plan.id),
        agent_id=str(plan.agent_id),
        tenant_id=_sid(plan.tenant_id),
        session_id=plan.session_id,
        runtime_task_id=_sid(plan.runtime_task_id),
        requested_by_user_id=_sid(plan.requested_by_user_id),
        source=plan.source,
        intent_type=plan.intent_type,
        original_request=plan.original_request,
        status=plan.status,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
        plan_markdown_path=plan.plan_markdown_path,
        plan_json=plan.plan_json or {},
        handoff_status=plan.handoff_status,
        handoff_payload=plan.handoff_payload,
        confirmed_by_user_id=_sid(plan.confirmed_by_user_id),
        confirmed_at=_dt(plan.confirmed_at),
        rejected_by_user_id=_sid(plan.rejected_by_user_id),
        rejected_at=_dt(plan.rejected_at),
        superseded_by_plan_id=_sid(plan.superseded_by_plan_id),
        expires_at=_dt(plan.expires_at),
        created_at=_dt(plan.created_at),
        updated_at=_dt(plan.updated_at),
        metadata=plan.metadata_json or {},
    )


def _recommendation_out(recommendation: AgentPlanRecommendation) -> PlanRecommendationOut:
    return PlanRecommendationOut(
        id=str(recommendation.id),
        agent_id=str(recommendation.agent_id),
        tenant_id=_sid(recommendation.tenant_id),
        session_id=recommendation.session_id,
        runtime_task_id=_sid(recommendation.runtime_task_id),
        recommended_to_user_id=str(recommendation.recommended_to_user_id),
        source=recommendation.source,
        intent_type=recommendation.intent_type,
        action_kind=recommendation.action_kind,
        tool_name=recommendation.tool_name,
        title=recommendation.title,
        original_request=recommendation.original_request,
        status=recommendation.status,
        declined_by_user_id=_sid(recommendation.declined_by_user_id),
        declined_at=_dt(recommendation.declined_at),
        accepted_by_user_id=_sid(recommendation.accepted_by_user_id),
        accepted_at=_dt(recommendation.accepted_at),
        created_at=_dt(recommendation.created_at),
        updated_at=_dt(recommendation.updated_at),
        metadata=recommendation.metadata_json or {},
    )


async def _load_plan_for_agent(
    service: PlanModeService, *, agent_id: uuid.UUID, plan_id: uuid.UUID
) -> AgentPlanRequest:
    """Fetch a plan and enforce that it belongs to ``agent_id`` (404 otherwise).

    ``check_agent_access`` has already proven the caller may touch this agent;
    this guards against confirming/reading a plan via the wrong agent path.
    """
    plan = await service.get_plan(plan_id)
    if plan is None or str(plan.agent_id) != str(agent_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


async def _load_recommendation_for_agent(
    db: AsyncSession, *, agent_id: uuid.UUID, recommendation_id: uuid.UUID
) -> AgentPlanRecommendation:
    result = await db.execute(
        select(AgentPlanRecommendation).where(
            AgentPlanRecommendation.id == recommendation_id,
            AgentPlanRecommendation.agent_id == agent_id,
        )
    )
    recommendation = result.scalar_one_or_none()
    if recommendation is None:
        raise HTTPException(status_code=404, detail="Plan recommendation not found")
    return recommendation


# ---------------------------------------------------------------------------
# create / list / get
# ---------------------------------------------------------------------------


@router.post(
    "/{agent_id}/plan-recommendations",
    response_model=PlanRecommendationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_plan_recommendation_endpoint(
    agent_id: uuid.UUID,
    payload: PlanRecommendationCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record that Plan Mode was recommended to the authenticated user."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    recommendation = await create_plan_recommendation(
        db,
        agent_id=agent_id,
        recommended_to_user_id=getattr(current_user, "id", None),
        tenant_id=getattr(agent, "tenant_id", None),
        session_id=payload.session_id,
        source=payload.source,
        original_request=payload.original_request,
        title=payload.title,
        intent_type=payload.intent_type,
        action_kind=payload.action_kind,
        tool_name=payload.tool_name,
        metadata_json=payload.metadata or {},
    )
    if recommendation is None:
        raise HTTPException(status_code=400, detail="session_id and authenticated user are required")
    await db.commit()
    if hasattr(db, "refresh"):
        await db.refresh(recommendation)
    return _recommendation_out(recommendation)


@router.post(
    "/{agent_id}/plan-recommendations/{recommendation_id}/decline",
    response_model=PlanRecommendationOut,
)
async def decline_plan_recommendation_endpoint(
    agent_id: uuid.UUID,
    recommendation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a Plan Mode recommendation declined by the authenticated user."""
    await check_agent_access(db, current_user, agent_id)
    recommendation = await _load_recommendation_for_agent(db, agent_id=agent_id, recommendation_id=recommendation_id)
    if str(recommendation.recommended_to_user_id) != str(getattr(current_user, "id", None)):
        raise HTTPException(status_code=403, detail="Plan recommendation belongs to a different user")
    if recommendation.status != "recommended":
        raise HTTPException(status_code=409, detail={"error": "invalid_status", "status": recommendation.status})
    recommendation = decline_recommendation(recommendation, user_id=current_user.id)
    await db.commit()
    if hasattr(db, "refresh"):
        await db.refresh(recommendation)
    return _recommendation_out(recommendation)


@router.post("/{agent_id}/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(
    agent_id: uuid.UUID,
    payload: PlanCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a plan request and generate its first version (§7 + §10.2)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    try:
        plan = await service.create_plan_request(
            agent_id=agent_id,
            requested_by_user_id=getattr(current_user, "id", None),
            original_request=payload.original_request,
            intent_type=payload.intent_type,
            source=payload.source,
            tenant_id=getattr(agent, "tenant_id", None),
            session_id=payload.session_id,
            metadata_json=payload.metadata or {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    plan = await _author_draft_plan(service, plan, fill=payload.fill)
    return _plan_out(plan)


@router.get("/{agent_id}/plans", response_model=list[PlanOut])
async def list_plans(
    agent_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List an agent's plan requests, newest first."""
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    plans = await service.list_plans_for_agent(agent_id, limit=limit)
    return [_plan_out(plan) for plan in plans]


@router.get("/{agent_id}/plans/{plan_id}", response_model=PlanOut)
async def get_plan(
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single plan request."""
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    plan = await _load_plan_for_agent(service, agent_id=agent_id, plan_id=plan_id)
    return _plan_out(plan)


@router.post("/{agent_id}/plans/{plan_id}/verify", response_model=PlanVerificationOut)
async def verify_plan(
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
    payload: PlanVerifyIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify claimed execution against the plan's own success criteria."""
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    plan = await _load_plan_for_agent(service, agent_id=agent_id, plan_id=plan_id)
    verification = verify_plan_artifact(
        plan_json=plan.plan_json or {},
        evidence_refs=payload.evidence_refs,
        completed_criteria=payload.completed_criteria,
        failed_criteria=payload.failed_criteria,
        notes=payload.notes,
    )
    metadata = dict(plan.metadata_json or {})
    metadata["last_verification"] = verification
    plan.metadata_json = metadata
    if hasattr(db, "commit"):
        await db.commit()
    return PlanVerificationOut(ok=True, plan_id=str(plan.id), verification=verification)


# ---------------------------------------------------------------------------
# revise / confirm / reject / handoff
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/plans/{plan_id}/revise", response_model=PlanOut)
async def revise_plan(
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
    payload: PlanReviseIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Supersede a plan with a new version and re-author it (§8.3).

    Cut ④: supersede to a fresh draft, then the agent authors the new version in a
    main-loop Plan Mode run (fills the draft via exit_plan_mode) — the single plan
    path. The legacy ``revise_plan`` (RPC planner) was removed.
    """
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    await _load_plan_for_agent(service, agent_id=agent_id, plan_id=plan_id)
    draft = await service.supersede_to_draft(plan_id=plan_id)
    new_plan = await _author_draft_plan(service, draft, fill=payload.fill)
    return _plan_out(new_plan)


@router.post("/{agent_id}/plans/{plan_id}/regenerate", response_model=PlanOut)
async def regenerate_plan(
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
    payload: PlanReviseIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retry generation for the same draft/planning_failed plan.

    This keeps chat inline plan cards recoverable: the card can refetch the same
    ``plan_id`` instead of losing the user on a superseded failed version.
    """
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    existing = await _load_plan_for_agent(service, agent_id=agent_id, plan_id=plan_id)
    try:
        plan = await _author_draft_plan(service, existing, fill=payload.fill, plan_id=plan_id)
    except PlanConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.error_code, "message": exc.message}) from exc
    return _plan_out(plan)


@router.post("/{agent_id}/plans/{plan_id}/confirm", response_model=PlanConfirmOut)
async def confirm_plan(
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
    payload: PlanConfirmIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm a specific plan version (§8.1 + §8.2 + §8.5).

    The body binds the confirmation to ``plan_version`` + ``plan_hash``; the
    confirming user is the authenticated user (never the request body), so an
    agent cannot confirm on a user's behalf.
    """
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    await _load_plan_for_agent(service, agent_id=agent_id, plan_id=plan_id)
    try:
        plan = await service.confirm_plan(
            plan_id=plan_id,
            confirming_user_id=getattr(current_user, "id", None),
            plan_version=payload.plan_version,
            plan_hash=payload.plan_hash,
            reason=payload.reason,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PlanConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.error_code, "message": exc.message}) from exc
    return PlanConfirmOut(ok=True, status=plan.status, plan_id=str(plan.id), handoff_status=plan.handoff_status)


@router.post("/{agent_id}/plans/{plan_id}/confirm-and-handoff", response_model=PlanHandoffOut)
async def confirm_and_handoff_plan(
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
    payload: PlanConfirmIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm a specific plan version and start its handoff in one backend call.

    This is the Web PlanCard happy path: avoid a browser-side split where
    ``confirm`` succeeds but the follow-up ``handoff`` request is lost.
    """
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    await _load_plan_for_agent(service, agent_id=agent_id, plan_id=plan_id)
    try:
        plan = await service.confirm_and_handoff_plan(
            plan_id=plan_id,
            confirming_user_id=getattr(current_user, "id", None),
            plan_version=payload.plan_version,
            plan_hash=payload.plan_hash,
            reason=payload.reason,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PlanConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.error_code, "message": exc.message}) from exc
    return PlanHandoffOut(
        ok=True,
        status=plan.status,
        plan_id=str(plan.id),
        handoff_status=plan.handoff_status,
        handoff_payload=plan.handoff_payload,
    )


@router.post("/{agent_id}/plans/{plan_id}/reject", response_model=PlanOut)
async def reject_plan(
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
    payload: PlanDecisionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reject an awaiting plan (§7, terminal)."""
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    await _load_plan_for_agent(service, agent_id=agent_id, plan_id=plan_id)
    try:
        plan = await service.reject_plan(
            plan_id=plan_id,
            rejecting_user_id=getattr(current_user, "id", None),
            reason=payload.reason,
        )
    except PlanConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.error_code, "message": exc.message}) from exc
    return _plan_out(plan)


@router.post("/{agent_id}/plans/{plan_id}/handoff", response_model=PlanHandoffOut)
async def handoff_plan(
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hand a confirmed plan to its execution target (§13).

    ``status`` stays ``confirmed``; the outcome is on ``handoff_status``.
    """
    await check_agent_access(db, current_user, agent_id)
    service = get_plan_mode_service()
    await _load_plan_for_agent(service, agent_id=agent_id, plan_id=plan_id)
    try:
        plan = await service.handoff_confirmed_plan(plan_id=plan_id)
    except PlanConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.error_code, "message": exc.message}) from exc
    return PlanHandoffOut(
        ok=True,
        status=plan.status,
        plan_id=str(plan.id),
        handoff_status=plan.handoff_status,
        handoff_payload=plan.handoff_payload,
    )
