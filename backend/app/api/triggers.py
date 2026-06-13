"""Triggers REST API — CRUD endpoints for the Aware page frontend."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.plan_mode_core import (
    plan_mode_user_declined,
    stamp_confirmed_plan_provenance,
    stamp_user_declined_plan_exemption,
)
from app.services.plan_mode_recommendation_service import (
    PlanRecommendationError,
    require_declined_plan_recommendation,
)
from app.services.agent_tool_domains.triggers import (
    VALID_TRIGGER_TYPES,
    _coerce_int,
    _parse_expires_at,
    _resolve_trigger_class,
    _validate_trigger_config,
    _validate_trigger_lifecycle_policy,
)
from app.services.autonomy_overview import build_trigger_view

router = APIRouter(prefix="/agents", tags=["triggers"])


class TriggerResponse(BaseModel):
    id: str
    name: str
    type: str
    config: dict
    reason: str
    is_enabled: bool
    fire_count: int
    max_fires: int | None = None
    cooldown_seconds: int
    last_fired_at: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    display_kind: str | None = None
    display_title: str | None = None
    display_schedule: str | None = None
    attention_state: str | None = None
    attention_reason: str | None = None
    next_action: str | None = None
    last_attempt: dict | None = None
    last_artifact: dict | None = None
    diagnostics: dict | None = None


class TriggerCreate(BaseModel):
    name: str
    type: str
    config: dict
    reason: str
    trigger_class: str | None = None
    max_fires: int | None = None
    cooldown_seconds: int | None = None
    expires_at: str | None = None
    # Plan Mode (§9.3): a confirmed plan authorising this autonomous wake.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None
    plan_mode_decision: str | None = None
    plan_recommendation_id: str | None = None


class TriggerUpdate(BaseModel):
    config: dict | None = None
    reason: str | None = None
    is_enabled: bool | None = None
    trigger_class: str | None = None
    max_fires: int | None = None
    cooldown_seconds: int | None = None
    expires_at: str | None = None
    # Plan Mode (§9.3): a confirmed plan authorising enabling this trigger.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None
    plan_mode_decision: str | None = None
    plan_recommendation_id: str | None = None


def _trigger_response(trigger: AgentTrigger, *, diagnostics: bool = False, view: dict | None = None) -> TriggerResponse:
    view = view or build_trigger_view(trigger, include_diagnostics=diagnostics)
    return TriggerResponse(
        id=str(trigger.id),
        name=trigger.name,
        type=trigger.type,
        config=trigger.config or {},
        reason=trigger.reason or "",
        is_enabled=bool(trigger.is_enabled),
        fire_count=int(trigger.fire_count or 0),
        max_fires=trigger.max_fires,
        cooldown_seconds=int(trigger.cooldown_seconds or 60),
        last_fired_at=trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
        created_at=trigger.created_at.isoformat() if trigger.created_at else None,
        expires_at=trigger.expires_at.isoformat() if trigger.expires_at else None,
        display_kind=view.get("display_kind"),
        display_title=view.get("display_title"),
        display_schedule=view.get("display_schedule"),
        attention_state=view.get("attention_state"),
        attention_reason=view.get("attention_reason"),
        next_action=view.get("next_action"),
        last_attempt=view.get("last_attempt"),
        last_artifact=view.get("last_artifact"),
        diagnostics=view.get("diagnostics") if diagnostics else None,
    )


def _plan_recommendation_error(exc: PlanRecommendationError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "ok": False,
            "status": "plan_recommendation_required",
            "error": exc.error_code,
            "summary": exc.message,
        },
    )


def _stamp_recommendation_exemption(config: dict, recommendation_id: object) -> dict:
    stamped = stamp_user_declined_plan_exemption(config)
    metadata = dict(stamped.get("metadata") or {})
    metadata["plan_recommendation_id"] = str(recommendation_id)
    stamped["metadata"] = metadata
    return stamped


def _raise_trigger_validation_error(error: str | None) -> None:
    if error:
        raise HTTPException(status_code=400, detail=error)


@router.get("/{agent_id}/triggers", response_model=list[TriggerResponse])
async def list_agent_triggers(
    agent_id: uuid.UUID,
    diagnostics: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all triggers for an agent."""
    include_diagnostics = diagnostics is True
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentTrigger)
        .where(AgentTrigger.agent_id == agent_id)
        .order_by(AgentTrigger.created_at.desc())
    )
    triggers = result.scalars().all()

    return [
        _trigger_response(t, diagnostics=include_diagnostics)
        for t in triggers
    ]


@router.post("/{agent_id}/triggers", response_model=TriggerResponse, status_code=status.HTTP_201_CREATED)
async def create_trigger(
    agent_id: uuid.UUID,
    body: TriggerCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a trigger from the frontend without routing through the agent tool loop."""
    await check_agent_access(db, current_user, agent_id)
    name = body.name.strip()
    reason = body.reason.strip()
    trigger_type = body.type.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Trigger name is required")
    if not reason:
        raise HTTPException(status_code=400, detail="Trigger reason is required")
    if trigger_type not in VALID_TRIGGER_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid trigger type: {trigger_type}")

    config = dict(body.config or {})
    if body.trigger_class is not None:
        config["trigger_class"] = body.trigger_class

    _raise_trigger_validation_error(_validate_trigger_config("create_trigger", trigger_type, config))
    _trigger_class, error = _resolve_trigger_class(
        "create_trigger",
        {"trigger_class": body.trigger_class},
        config,
        trigger_type=trigger_type,
    )
    _raise_trigger_validation_error(error)
    _raise_trigger_validation_error(
        _validate_trigger_lifecycle_policy(
            "create_trigger",
            trigger_type,
            config,
            {"max_fires": body.max_fires, "expires_at": body.expires_at},
        )
    )

    # §9 P8: a workflow_ref pins a registered definition at CREATION time —
    # the pinned name/version/hash must exist, be executable by this agent,
    # and hash-match right now, or the trigger is refused.
    if isinstance(config.get("workflow_ref"), dict):
        from app.services.workflow_trigger import validate_workflow_ref_for_trigger

        ref_error = await validate_workflow_ref_for_trigger(
            tenant_id=current_user.tenant_id,
            agent_id=agent_id,
            ref=config["workflow_ref"],
        )
        if ref_error:
            raise HTTPException(status_code=400, detail=f"Invalid workflow_ref: {ref_error}")
    try:
        expires_at = _parse_expires_at(body.expires_at) if body.expires_at else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid expires_at: {body.expires_at}") from exc

    # Plan Mode early intercept (§9.3): this endpoint creates an enabled
    # autonomous trigger. A confirmed plan is required unless the caller carries
    # an explicit user opt-out bound to a declined recommendation row.
    user_declined_plan_mode = plan_mode_user_declined(body.plan_mode_decision)
    if not user_declined_plan_mode:
        await enforce_plan_gate(
            db,
            agent_id=agent_id,
            action_kind="create_enabled_trigger",
            gate=get_plan_mode_gate(),
            confirmed_plan_id=body.confirmed_plan_id,
            confirmed_plan_version=body.confirmed_plan_version,
            confirmed_plan_hash=body.confirmed_plan_hash,
            action_artifact={"config": config},
        )
        config = stamp_confirmed_plan_provenance(
            config,
            plan_id=body.confirmed_plan_id,
            plan_version=body.confirmed_plan_version,
            plan_hash=body.confirmed_plan_hash,
        )
    else:
        try:
            recommendation = await require_declined_plan_recommendation(
                db,
                recommendation_id=body.plan_recommendation_id,
                agent_id=agent_id,
                user_id=getattr(current_user, "id", None),
                action_kind="create_enabled_trigger",
            )
        except PlanRecommendationError as exc:
            raise _plan_recommendation_error(exc) from exc
        config = _stamp_recommendation_exemption(config, recommendation.id)

    trigger = AgentTrigger(
        agent_id=agent_id,
        name=name,
        type=trigger_type,
        config=config,
        reason=reason,
        is_enabled=True,
        max_fires=_coerce_int(body.max_fires),
        cooldown_seconds=_coerce_int(body.cooldown_seconds) or 60,
        expires_at=expires_at,
    )
    db.add(trigger)
    await db.commit()
    if hasattr(db, "refresh"):
        await db.refresh(trigger)
    return _trigger_response(trigger)


@router.patch("/{agent_id}/triggers/{trigger_id}")
async def update_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    body: TriggerUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a trigger (from frontend management UI)."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.id == trigger_id,
            AgentTrigger.agent_id == agent_id,
        )
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(404, "Trigger not found")

    # Plan Mode early intercept (§9.3): only enabling an autonomous wake is
    # gated. Disables, config edits, and reason/lifecycle changes are low-risk
    # and keep their existing contract.
    user_declined_plan_mode = plan_mode_user_declined(body.plan_mode_decision)
    recommendation = None
    if body.is_enabled is True:
        if user_declined_plan_mode:
            try:
                recommendation = await require_declined_plan_recommendation(
                    db,
                    recommendation_id=body.plan_recommendation_id,
                    agent_id=agent_id,
                    user_id=getattr(current_user, "id", None),
                    action_kind="enable_autonomous_wake",
                )
            except PlanRecommendationError as exc:
                raise _plan_recommendation_error(exc) from exc
        else:
            await enforce_plan_gate(
                db,
                agent_id=agent_id,
                action_kind="enable_autonomous_wake",
                gate=get_plan_mode_gate(),
                confirmed_plan_id=body.confirmed_plan_id,
                confirmed_plan_version=body.confirmed_plan_version,
                confirmed_plan_hash=body.confirmed_plan_hash,
                action_artifact={"config": body.config if body.config is not None else trigger.config},
            )

    if body.config is not None:
        trigger.config = body.config
    if body.is_enabled is True:
        if user_declined_plan_mode:
            trigger.config = _stamp_recommendation_exemption(trigger.config, recommendation.id)
        else:
            trigger.config = stamp_confirmed_plan_provenance(
                trigger.config,
                plan_id=body.confirmed_plan_id,
                plan_version=body.confirmed_plan_version,
                plan_hash=body.confirmed_plan_hash,
            )
    if body.reason is not None:
        trigger.reason = body.reason
    if body.is_enabled is not None:
        trigger.is_enabled = body.is_enabled
    if body.max_fires is not None:
        trigger.max_fires = body.max_fires
    if body.cooldown_seconds is not None:
        trigger.cooldown_seconds = body.cooldown_seconds
    if body.expires_at is not None:
        from datetime import datetime

        trigger.expires_at = datetime.fromisoformat(body.expires_at)
    if body.trigger_class is not None:
        config = dict(trigger.config or {})
        config["trigger_class"] = body.trigger_class
        _trigger_class, error = _resolve_trigger_class(
            "update_trigger",
            {"trigger_class": body.trigger_class},
            config,
            trigger_type=trigger.type,
        )
        _raise_trigger_validation_error(error)
        _raise_trigger_validation_error(
            _validate_trigger_lifecycle_policy(
                "update_trigger",
                trigger.type,
                config,
                {"max_fires": trigger.max_fires, "expires_at": trigger.expires_at},
            )
        )
        trigger.config = config

    await db.commit()

    return {"ok": True}


@router.delete("/{agent_id}/triggers/{trigger_id}")
async def delete_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a trigger entirely."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.id == trigger_id,
            AgentTrigger.agent_id == agent_id,
        )
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(404, "Trigger not found")

    await db.delete(trigger)
    await db.commit()

    return {"ok": True}
