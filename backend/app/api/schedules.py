"""Legacy schedules API backed by AgentTrigger cron wake policies."""

import uuid
from datetime import datetime, timezone

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate, stamp_plan_gate_decision
from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.resource_authority import authorize_resource_action, filter_authorized_resources
from app.core.security import get_current_user
from app.database import get_db
from app.models.activity_log import AgentActivityLog
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.plan_mode_core import (
    plan_mode_user_declined,
    stamp_user_declined_plan_exemption,
)
from app.services.plan_mode_recommendation_service import (
    PlanRecommendationError,
    require_declined_plan_recommendation,
)
from app.services.channel_secret_storage import redact_delivery_target

router = APIRouter(prefix="/agents/{agent_id}/schedules", tags=["schedules"])


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    instruction: str = Field(default="", max_length=5000)
    cron_expr: str = Field(min_length=1, max_length=100)
    is_enabled: bool = True
    delivery_target_json: dict | None = None
    # Plan Mode (§9.3): a confirmed plan authorising this autonomous wake.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None
    confirmed_plan_session_id: str | None = None
    plan_mode_decision: str | None = None
    plan_recommendation_id: str | None = None


class ScheduleUpdate(BaseModel):
    name: str | None = None
    instruction: str | None = None
    cron_expr: str | None = None
    is_enabled: bool | None = None
    delivery_target_json: dict | None = None
    # Plan Mode (§9.3): a confirmed plan authorising enabling this schedule.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None
    confirmed_plan_session_id: str | None = None
    plan_mode_decision: str | None = None
    plan_recommendation_id: str | None = None


class ScheduleRunIn(BaseModel):
    # Plan Mode (§9.3): a confirmed plan authorising this one-shot autonomous run.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None
    confirmed_plan_session_id: str | None = None
    plan_mode_decision: str | None = None
    plan_recommendation_id: str | None = None


class ScheduleOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    instruction: str
    cron_expr: str
    is_enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int
    created_by: uuid.UUID | None = None
    creator_username: str | None = None
    delivery_target_json: dict | None = None
    created_at: datetime | None = None
    authority_source: str | None = None
    operator_view: bool = False

    model_config = {"from_attributes": True}


def compute_next_run(cron_expr: str, after: datetime | None = None) -> datetime | None:
    try:
        base = after or datetime.now(timezone.utc)
        return croniter(cron_expr, base).get_next(datetime).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _trigger_config(trigger: AgentTrigger) -> dict:
    return dict(trigger.config or {})


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


def _created_by_from_config(config: dict) -> uuid.UUID | None:
    raw = config.get("created_by")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _root_session_from_config(config: dict) -> uuid.UUID | None:
    raw = config.get("root_session_id") or config.get("confirmed_plan_session_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _authority_state_from_config(config: dict) -> str:
    state = str(config.get("authority_state") or "").strip().lower()
    if state in {"owned", "quarantined"}:
        return state
    return "owned" if _created_by_from_config(config) else "quarantined"


def _schedule_out(
    trigger: AgentTrigger,
    *,
    creator_username: str | None = None,
    authority_source: str | None = None,
    operator_view: bool = False,
) -> ScheduleOut:
    config = _trigger_config(trigger)
    cron_expr = str(config.get("expr") or "")
    return ScheduleOut(
        id=trigger.id,
        agent_id=trigger.agent_id,
        name=trigger.name,
        instruction=trigger.reason or "",
        cron_expr=cron_expr,
        is_enabled=bool(trigger.is_enabled),
        last_run_at=trigger.last_fired_at,
        next_run_at=compute_next_run(cron_expr, trigger.last_fired_at or trigger.created_at)
        if trigger.is_enabled
        else None,
        run_count=int(trigger.fire_count or 0),
        created_by=_created_by_from_config(config),
        creator_username=creator_username,
        delivery_target_json=redact_delivery_target(trigger.reply_context or config.get("delivery_target_json")),
        created_at=trigger.created_at,
        authority_source=authority_source,
        operator_view=operator_view,
    )


def _schedule_config(
    cron_expr: str,
    *,
    created_by: uuid.UUID | None = None,
    root_session_id: uuid.UUID | str | None = None,
    delivery_target_json: dict | None = None,
) -> dict:
    config = {
        "expr": cron_expr,
        "trigger_class": "scheduled_job",
        "legacy_surface": "schedules_api",
    }
    if created_by:
        config["created_by"] = str(created_by)
        config["authority_state"] = "owned"
    else:
        config["authority_state"] = "quarantined"
    if root_session_id:
        config["root_session_id"] = str(root_session_id)
    if delivery_target_json is not None:
        config["delivery_target_json"] = delivery_target_json
    return config


async def _load_schedule_trigger(db: AsyncSession, agent_id: uuid.UUID, schedule_id: uuid.UUID) -> AgentTrigger:
    result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.id == schedule_id,
            AgentTrigger.agent_id == agent_id,
            AgentTrigger.type == "cron",
        )
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return trigger


async def _authorize_schedule(
    db: AsyncSession,
    current_user: User,
    *,
    agent_id: uuid.UUID,
    trigger: AgentTrigger,
    action: str,
    agent_access,
    operator_view: bool,
    operator_reason: str | None,
):
    config = _trigger_config(trigger)
    return await authorize_resource_action(
        db,
        current_user,
        agent_id=agent_id,
        resource_kind="schedule",
        resource_id=trigger.id,
        action=action,
        owner_user_id=_created_by_from_config(config),
        root_session_id=_root_session_from_config(config),
        authority_state=_authority_state_from_config(config),
        allow_manager_override=operator_view,
        manager_override_reason=operator_reason,
        agent_access=agent_access,
    )


@router.get("/", response_model=list[ScheduleOut])
async def list_schedules(
    agent_id: uuid.UUID,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List cron wake policies through the legacy schedules surface."""
    agent_access = await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentTrigger)
        .where(AgentTrigger.agent_id == agent_id, AgentTrigger.type == "cron")
        .order_by(AgentTrigger.created_at.desc())
    )
    triggers = result.scalars().all()
    authorized = await filter_authorized_resources(
        db,
        current_user,
        agent_id=agent_id,
        resource_kind="schedule",
        action="read",
        resources=triggers,
        owner_user_id_of=lambda trigger: _created_by_from_config(_trigger_config(trigger)),
        root_session_id_of=lambda trigger: _root_session_from_config(_trigger_config(trigger)),
        authority_state_of=lambda trigger: _authority_state_from_config(_trigger_config(trigger)),
        operator_view=operator_view,
        operator_reason=operator_reason,
        agent_access=agent_access,
    )
    creator_ids = {
        created_by
        for trigger, _decision in authorized
        if (created_by := _created_by_from_config(_trigger_config(trigger))) is not None
    }
    creator_map = {}
    if creator_ids:
        users_result = await db.execute(select(User).where(User.id.in_(creator_ids)))
        creator_map = {user.id: user.username for user in users_result.scalars().all()}
    return [
        _schedule_out(
            trigger,
            creator_username=creator_map.get(_created_by_from_config(_trigger_config(trigger))),
            authority_source=decision.authority_source,
            operator_view=decision.operator_view,
        )
        for trigger, decision in authorized
    ]


@router.post("/", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    agent_id: uuid.UUID,
    data: ScheduleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a scheduled wake policy as an AgentTrigger."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    if not compute_next_run(data.cron_expr):
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {data.cron_expr}")

    # Plan Mode early intercept (§9.3): an enabled cron schedule is an
    # autonomous wake. A confirmed plan is required unless the caller carries an
    # explicit user opt-out bound to a declined recommendation row. A disabled draft is
    # not gated.
    user_declined_plan_mode = plan_mode_user_declined(data.plan_mode_decision)
    config = _schedule_config(
        data.cron_expr,
        created_by=current_user.id,
        root_session_id=data.confirmed_plan_session_id,
        delivery_target_json=data.delivery_target_json,
    )
    plan_decision = None
    evidence_id = f"schedule-create:{uuid.uuid4()}"
    if data.is_enabled and not user_declined_plan_mode:
        plan_decision = await enforce_plan_gate(
            db,
            agent_id=agent_id,
            requester_user_id=current_user.id,
            session_id=data.confirmed_plan_session_id,
            action_kind="create_enabled_trigger",
            target_ref="schedule:new",
            gate=get_plan_mode_gate(),
            confirmed_plan_id=data.confirmed_plan_id,
            confirmed_plan_version=data.confirmed_plan_version,
            confirmed_plan_hash=data.confirmed_plan_hash,
            action_artifact={
                "name": data.name.strip(),
                "instruction": data.instruction,
                "cron_expr": data.cron_expr,
                "is_enabled": data.is_enabled,
                "delivery_target_json": data.delivery_target_json,
                "config": config,
            },
            evidence_id=evidence_id,
        )
    recommendation = None
    if data.is_enabled and user_declined_plan_mode:
        try:
            recommendation = await require_declined_plan_recommendation(
                db,
                recommendation_id=data.plan_recommendation_id,
                agent_id=agent_id,
                user_id=getattr(current_user, "id", None),
                action_kind="create_enabled_trigger",
            )
        except PlanRecommendationError as exc:
            raise _plan_recommendation_error(exc) from exc

    if data.is_enabled:
        if user_declined_plan_mode:
            config = _stamp_recommendation_exemption(config, recommendation.id)
        else:
            config = stamp_plan_gate_decision(
                config,
                decision=plan_decision,
                confirmed_plan_id=data.confirmed_plan_id,
                confirmed_plan_version=data.confirmed_plan_version,
                confirmed_plan_hash=data.confirmed_plan_hash,
                requester_user_id=current_user.id,
                session_id=data.confirmed_plan_session_id,
                evidence_id=evidence_id,
            )

    trigger = AgentTrigger(
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        name=data.name.strip(),
        type="cron",
        config=config,
        reason=data.instruction,
        reply_context=data.delivery_target_json,
        is_enabled=data.is_enabled,
        cooldown_seconds=60,
    )
    db.add(trigger)
    await db.flush()
    return _schedule_out(trigger, creator_username=current_user.username)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    data: ScheduleUpdate,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a scheduled wake policy."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    trigger = await _load_schedule_trigger(db, agent_id, schedule_id)
    decision = await _authorize_schedule(
        db,
        current_user,
        agent_id=agent_id,
        trigger=trigger,
        action="write",
        agent_access=(agent, "manage"),
        operator_view=operator_view,
        operator_reason=operator_reason,
    )
    updates = data.model_dump(exclude_unset=True)
    # Plan Mode early intercept (§9.3): only re-enabling a schedule is gated,
    # unless the user explicitly declined a bound recommendation.
    user_declined_plan_mode = plan_mode_user_declined(data.plan_mode_decision)
    recommendation = None
    plan_decision = None
    evidence_id = f"schedule-enable:{trigger.id}:{uuid.uuid4()}"
    if updates.get("is_enabled") is True:
        if user_declined_plan_mode:
            try:
                recommendation = await require_declined_plan_recommendation(
                    db,
                    recommendation_id=data.plan_recommendation_id,
                    agent_id=agent_id,
                    user_id=getattr(current_user, "id", None),
                    action_kind="enable_autonomous_wake",
                )
            except PlanRecommendationError as exc:
                raise _plan_recommendation_error(exc) from exc
        else:
            plan_decision = await enforce_plan_gate(
                db,
                agent_id=agent_id,
                requester_user_id=current_user.id,
                session_id=data.confirmed_plan_session_id,
                action_kind="enable_autonomous_wake",
                target_ref=f"schedule:{trigger.id}",
                gate=get_plan_mode_gate(),
                confirmed_plan_id=data.confirmed_plan_id,
                confirmed_plan_version=data.confirmed_plan_version,
                confirmed_plan_hash=data.confirmed_plan_hash,
                action_artifact={
                    "schedule_id": str(trigger.id),
                    "updates": data.model_dump(mode="json", exclude_unset=True),
                    "current_config": _trigger_config(trigger),
                },
                evidence_id=evidence_id,
            )
    if "name" in updates and updates["name"] is not None:
        trigger.name = str(updates["name"]).strip()
    if "instruction" in updates and updates["instruction"] is not None:
        trigger.reason = updates["instruction"]
    if "is_enabled" in updates and updates["is_enabled"] is not None:
        trigger.is_enabled = bool(updates["is_enabled"])
    if "delivery_target_json" in updates:
        trigger.reply_context = updates["delivery_target_json"]

    config = _trigger_config(trigger)
    if "cron_expr" in updates and updates["cron_expr"] is not None:
        if not compute_next_run(updates["cron_expr"]):
            raise HTTPException(status_code=400, detail=f"Invalid cron expression: {updates['cron_expr']}")
        config["expr"] = updates["cron_expr"]
    if "delivery_target_json" in updates:
        config["delivery_target_json"] = updates["delivery_target_json"]
    config["trigger_class"] = "scheduled_job"
    config["legacy_surface"] = "schedules_api"
    if updates.get("is_enabled") is True:
        if user_declined_plan_mode:
            config = _stamp_recommendation_exemption(config, recommendation.id)
        else:
            config = stamp_plan_gate_decision(
                config,
                decision=plan_decision,
                confirmed_plan_id=data.confirmed_plan_id,
                confirmed_plan_version=data.confirmed_plan_version,
                confirmed_plan_hash=data.confirmed_plan_hash,
                requester_user_id=current_user.id,
                session_id=data.confirmed_plan_session_id,
                evidence_id=evidence_id,
            )
    trigger.config = config

    await db.flush()
    return _schedule_out(
        trigger,
        authority_source=decision.authority_source,
        operator_view=decision.operator_view,
    )


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a scheduled wake policy."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    trigger = await _load_schedule_trigger(db, agent_id, schedule_id)
    await _authorize_schedule(
        db,
        current_user,
        agent_id=agent_id,
        trigger=trigger,
        action="delete",
        agent_access=(agent, "manage"),
        operator_view=operator_view,
        operator_reason=operator_reason,
    )
    await db.delete(trigger)
    await db.flush()


@router.post("/{schedule_id}/run")
async def trigger_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    data: ScheduleRunIn | None = None,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Queue a one-shot trigger instead of directly invoking a scheduler runtime."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    trigger = await _load_schedule_trigger(db, agent_id, schedule_id)
    await _authorize_schedule(
        db,
        current_user,
        agent_id=agent_id,
        trigger=trigger,
        action="execute",
        agent_access=(agent, _access),
        operator_view=operator_view,
        operator_reason=operator_reason,
    )
    trigger_tenant_id = getattr(trigger, "tenant_id", None)
    if trigger_tenant_id is None:
        trigger.tenant_id = agent.tenant_id
        trigger_tenant_id = agent.tenant_id
    # Plan Mode early intercept (§9.3): a manual run queues an enabled one-shot
    # autonomous trigger, so it needs a confirmed plan (or cutover exemption).
    run = data or ScheduleRunIn()
    user_declined_plan_mode = plan_mode_user_declined(run.plan_mode_decision)
    plan_decision = None
    evidence_id = f"schedule-run:{schedule_id}:{uuid.uuid4()}"
    if not user_declined_plan_mode:
        plan_decision = await enforce_plan_gate(
            db,
            agent_id=agent_id,
            requester_user_id=current_user.id,
            session_id=run.confirmed_plan_session_id,
            action_kind="create_enabled_trigger",
            target_ref=f"schedule:{schedule_id}:run",
            gate=get_plan_mode_gate(),
            confirmed_plan_id=run.confirmed_plan_id,
            confirmed_plan_version=run.confirmed_plan_version,
            confirmed_plan_hash=run.confirmed_plan_hash,
            action_artifact={
                "schedule_id": str(schedule_id),
                "source_config": _trigger_config(trigger),
                "instruction": trigger.reason,
            },
            evidence_id=evidence_id,
        )
    recommendation = None
    if user_declined_plan_mode:
        try:
            recommendation = await require_declined_plan_recommendation(
                db,
                recommendation_id=run.plan_recommendation_id,
                agent_id=agent_id,
                user_id=getattr(current_user, "id", None),
                action_kind="create_enabled_trigger",
            )
        except PlanRecommendationError as exc:
            raise _plan_recommendation_error(exc) from exc
    now = datetime.now(timezone.utc)
    manual_config = {
        "at": now.isoformat(),
        "trigger_class": "scheduled_job",
        "source_schedule_id": str(schedule_id),
        "legacy_surface": "schedules_api_manual_run",
        "created_by": str(current_user.id),
        "root_session_id": str(run.confirmed_plan_session_id) if run.confirmed_plan_session_id else None,
        "authority_state": "owned",
    }
    if user_declined_plan_mode:
        manual_config = _stamp_recommendation_exemption(manual_config, recommendation.id)
    else:
        manual_config = stamp_plan_gate_decision(
            manual_config,
            decision=plan_decision,
            confirmed_plan_id=run.confirmed_plan_id,
            confirmed_plan_version=run.confirmed_plan_version,
            confirmed_plan_hash=run.confirmed_plan_hash,
            requester_user_id=current_user.id,
            session_id=run.confirmed_plan_session_id,
            evidence_id=evidence_id,
        )
    manual = AgentTrigger(
        agent_id=agent_id,
        tenant_id=trigger_tenant_id or agent.tenant_id,
        name=f"manual_{trigger.name[:70]}_{uuid.uuid4().hex[:8]}",
        type="once",
        config=manual_config,
        reason=trigger.reason or f"Manual run for schedule {trigger.name}",
        reply_context=trigger.reply_context,
        is_enabled=True,
        max_fires=1,
        cooldown_seconds=0,
    )
    db.add(manual)
    await db.flush()
    return {"status": "queued", "schedule_id": str(schedule_id), "trigger_id": str(manual.id)}


@router.get("/{schedule_id}/history")
async def get_schedule_history(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get execution history for a legacy schedule id from activity logs."""
    agent_access = await check_agent_access(db, current_user, agent_id)
    trigger = await _load_schedule_trigger(db, agent_id, schedule_id)
    decision = await _authorize_schedule(
        db,
        current_user,
        agent_id=agent_id,
        trigger=trigger,
        action="read",
        agent_access=agent_access,
        operator_view=operator_view,
        operator_reason=operator_reason,
    )
    result = await db.execute(
        select(AgentActivityLog)
        .where(
            AgentActivityLog.agent_id == agent_id,
            AgentActivityLog.action_type.in_(["schedule_run", "trigger_run"]),
        )
        .order_by(AgentActivityLog.created_at.desc())
    )
    logs = result.scalars().all()
    history = []
    for log in logs:
        detail = log.detail_json or {}
        if detail.get("schedule_id") == str(schedule_id) or detail.get("source_schedule_id") == str(schedule_id):
            history.append(
                {
                    "id": str(log.id),
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                    "summary": log.summary,
                    "instruction": detail.get("instruction", ""),
                    "reply": detail.get("reply", ""),
                    "authority_source": decision.authority_source,
                    "operator_view": decision.operator_view,
                }
            )
        if len(history) >= 20:
            break
    return history
