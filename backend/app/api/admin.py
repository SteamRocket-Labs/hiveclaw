"""Platform Admin company management API.

Provides endpoints for platform admins to manage companies, view stats,
and control platform-level settings.
"""

import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func as sqla_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_role
from app.database import enter_rls_bypass, get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.invitation_code import InvitationCode
from app.models.system_settings import SystemSetting
from app.models.tenant import Tenant
from app.models.token_usage_event import TokenUsageEvent
from app.models.user import User
from app.services.autonomous_audit import build_autonomous_audit_report
from app.services.harness_canary import run_harness_canary
from app.services.harness_validation_report import build_harness_validation_report
from app.services.plan_mode_cutover import mark_existing_triggers_plan_exempt
from app.services.runtime_reconciliation import (
    RuntimeReconciliationConflict,
    RuntimeReconciliationNotFound,
    apply_runtime_reconciliation_action,
    get_runtime_reconciliation_task,
    list_runtime_reconciliation_tasks,
)
from app.services.workflow_ops import WorkflowOpsConflict, WorkflowOpsService
from app.services.workflow_runtime_service import WorkflowRunNotFound

router = APIRouter(prefix="/admin", tags=["admin"])


# ─── Schemas ────────────────────────────────────────────

class CompanyStats(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime | None = None
    user_count: int = 0
    agent_count: int = 0
    agent_running_count: int = 0
    total_tokens: int = 0


class CompanyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CompanyCreateResponse(BaseModel):
    company: CompanyStats
    admin_invitation_code: str


class PlatformSettingsOut(BaseModel):
    allow_self_create_company: bool = True
    invitation_code_enabled: bool = False


class PlatformSettingsUpdate(BaseModel):
    allow_self_create_company: bool | None = None
    invitation_code_enabled: bool | None = None


class HarnessCanaryRunRequest(BaseModel):
    tenant_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    include_h4: bool = True
    include_h5: bool = True
    max_agents: int = Field(default=50, ge=1, le=500)
    force: bool = False


class PlanModeCutoverRequest(BaseModel):
    dry_run: bool = Field(
        default=True,
        description="When true (default), count what would be stamped without writing the DB.",
    )
    agent_id: uuid.UUID | None = Field(
        default=None,
        description="Scope the cutover to a single agent. Omit to cover every enabled trigger.",
    )


class WorkflowAdminReasonRequest(BaseModel):
    reason: str = Field(default="operator requested", max_length=1000)


class WorkflowReplayFromStepRequest(BaseModel):
    step_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="operator requested", max_length=1000)


class RuntimeReconciliationActionRequest(BaseModel):
    action: str = Field(pattern="^(mark_resolved|archive|retry)$")
    reason: str = Field(min_length=1, max_length=1000)


async def _pin_admin_tenant_scope(db: AsyncSession, tenant_id: uuid.UUID | None) -> None:
    if tenant_id is not None:
        await pin_rls_tenant_context(db, tenant_id)


async def _load_platform_admin_agent_and_pin(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    admin_user: User,
    reason: str,
) -> Agent:
    async with enter_rls_bypass(
        db,
        reason=reason,
        actor_id=str(admin_user.id),
    ) as bypass_db:
        agent_row = (await bypass_db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if not agent_row:
        raise HTTPException(status_code=404, detail="agent not found")
    await _pin_admin_tenant_scope(db, agent_row.tenant_id)
    return agent_row


# ─── Company Management ────────────────────────────────

@router.get("/companies", response_model=list[CompanyStats])
async def list_companies(
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all companies with stats."""
    async with enter_rls_bypass(
        db,
        reason="platform admin company list and cross-tenant stats",
        actor_id=str(current_user.id),
    ) as bypass_db:
        tenants = await bypass_db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
        result = []

        for tenant in tenants.scalars().all():
            tid = tenant.id

            # User count
            uc = await bypass_db.execute(select(sqla_func.count()).select_from(User).where(User.tenant_id == tid))
            user_count = uc.scalar() or 0

            # Agent count
            ac = await bypass_db.execute(select(sqla_func.count()).select_from(Agent).where(Agent.tenant_id == tid))
            agent_count = ac.scalar() or 0

            # Running agents
            rc = await bypass_db.execute(
                select(sqla_func.count()).select_from(Agent).where(
                    Agent.tenant_id == tid,
                    Agent.status == "running",
                )
            )
            agent_running = rc.scalar() or 0

            # Total tokens
            tc = await bypass_db.execute(
                select(sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0)).where(Agent.tenant_id == tid)
            )
            total_tokens = tc.scalar() or 0

            result.append(
                CompanyStats(
                    id=tenant.id,
                    name=tenant.name,
                    slug=tenant.slug,
                    is_active=tenant.is_active,
                    created_at=tenant.created_at,
                    user_count=user_count,
                    agent_count=agent_count,
                    agent_running_count=agent_running,
                    total_tokens=total_tokens,
                )
            )

    return result


@router.post("/companies", response_model=CompanyCreateResponse, status_code=201)
async def create_company(
    data: CompanyCreateRequest,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new company and generate an admin invitation code (max_uses=1)."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", data.name.lower().strip()).strip("-")[:40]
    if not slug:
        slug = "company"
    slug = f"{slug}-{secrets.token_hex(3)}"

    tenant = Tenant(name=data.name, slug=slug, im_provider="web_only")
    db.add(tenant)
    await db.flush()
    await _pin_admin_tenant_scope(db, tenant.id)

    # Generate admin invitation code (single-use)
    code_str = secrets.token_urlsafe(12)[:16].upper()
    invite = InvitationCode(
        code=code_str,
        tenant_id=tenant.id,
        max_uses=1,
        created_by=current_user.id,
    )
    db.add(invite)
    await db.flush()

    return CompanyCreateResponse(
        company=CompanyStats(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            is_active=tenant.is_active,
            created_at=tenant.created_at,
        ),
        admin_invitation_code=code_str,
    )


_ALLOWED_NATIVE_MEMORY_VALUES = {"md", None}


class MemoryBackendUpdate(BaseModel):
    memory_backend: str | None = Field(
        default=None,
        description="Only 'md' or null are accepted. Native T3 Markdown is the memory backend.",
    )


@router.patch("/companies/{company_id}/memory-backend")
async def set_company_memory_backend(
    company_id: uuid.UUID,
    payload: MemoryBackendUpdate,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Persist the tenant's native Markdown memory preference.

    This endpoint is kept for admin compatibility, but Hive no longer exposes a
    concrete external T3 memory backend switch.
    """
    del current_user
    if payload.memory_backend not in _ALLOWED_NATIVE_MEMORY_VALUES:
        raise HTTPException(
            status_code=400,
            detail="memory_backend must be 'md' or null",
        )

    result = await db.execute(select(Tenant).where(Tenant.id == company_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    tenant.memory_backend = payload.memory_backend
    await db.flush()

    return {"ok": True, "memory_backend": tenant.memory_backend}


@router.get("/metrics/memory")
async def get_memory_backend_metrics(
    current_user: User = Depends(require_role("platform_admin")),
):
    """Return in-memory memory-backend counters (recall latency, errors, sync lag)."""
    del current_user
    from app.memory.metrics import snapshot
    return snapshot()


@router.get("/metrics/workflows")
async def get_workflow_metrics(
    current_user: User = Depends(require_role("platform_admin")),
):
    """Return in-process workflow runtime counters for rollout checks."""
    del current_user
    from app.services.workflow_metrics import snapshot_workflow_metrics

    return snapshot_workflow_metrics()


@router.get("/invocation-traces/{trace_id}")
async def get_invocation_trace(
    trace_id: str,
    tenant_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Return a persisted invocation span tree for operator debugging."""
    del current_user
    from app.services.invocation_trace import get_invocation_trace_tree

    await _pin_admin_tenant_scope(db, tenant_id)
    payload = await get_invocation_trace_tree(db, tenant_id=tenant_id, trace_id=trace_id)
    if payload["span_count"] == 0:
        raise HTTPException(status_code=404, detail="Invocation trace not found")
    return payload


@router.get("/runtime-reconciliation")
async def list_runtime_reconciliation(
    tenant_id: uuid.UUID = Query(...),
    status: str = Query(default="needs_reconciliation"),
    limit: int = Query(default=50, ge=1, le=200),
    agent_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List RuntimeTasks that require operator reconciliation."""
    del current_user
    await _pin_admin_tenant_scope(db, tenant_id)
    return await list_runtime_reconciliation_tasks(
        db,
        tenant_id=tenant_id,
        status=status,
        limit=limit,
        agent_id=agent_id,
    )


@router.get("/runtime-reconciliation/{task_id}")
async def get_runtime_reconciliation(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Inspect one RuntimeTask reconciliation record."""
    del current_user
    await _pin_admin_tenant_scope(db, tenant_id)
    payload = await get_runtime_reconciliation_task(db, task_id=task_id, tenant_id=tenant_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Runtime reconciliation task not found")
    return payload


@router.post("/runtime-reconciliation/{task_id}/action")
async def apply_runtime_reconciliation(
    task_id: uuid.UUID,
    payload: RuntimeReconciliationActionRequest,
    tenant_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Apply an operator reconciliation action to one RuntimeTask."""
    await _pin_admin_tenant_scope(db, tenant_id)
    try:
        return await apply_runtime_reconciliation_action(
            db,
            task_id=task_id,
            tenant_id=tenant_id,
            action=payload.action,
            reason=payload.reason,
            actor_user_id=current_user.id,
        )
    except RuntimeReconciliationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeReconciliationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/workflows/{run_id}")
async def inspect_workflow_run(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
):
    """Inspect a workflow RuntimeTask + step/quota journal."""
    del current_user
    try:
        return await WorkflowOpsService().inspect_run(run_id, tenant_id=tenant_id)
    except WorkflowRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workflows/{run_id}/journal")
async def export_workflow_journal(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
):
    """Export the workflow run journal including leaf calls."""
    del current_user
    try:
        return await WorkflowOpsService().export_journal(run_id, tenant_id=tenant_id)
    except WorkflowRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workflows/{run_id}/cancel")
async def cancel_workflow_run(
    run_id: uuid.UUID,
    payload: WorkflowAdminReasonRequest | None = None,
    tenant_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
):
    """Persistently kill a workflow run; killed runs are not auto-resumed."""
    del current_user
    payload = payload or WorkflowAdminReasonRequest()
    try:
        return await WorkflowOpsService().cancel_run(run_id, tenant_id=tenant_id, reason=payload.reason)
    except WorkflowRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workflows/{run_id}/force-suspend")
async def force_suspend_workflow_run(
    run_id: uuid.UUID,
    payload: WorkflowAdminReasonRequest | None = None,
    tenant_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
):
    """Force a run into suspended state for manual reconciliation."""
    del current_user
    payload = payload or WorkflowAdminReasonRequest()
    try:
        return await WorkflowOpsService().force_suspend_run(run_id, tenant_id=tenant_id, reason=payload.reason)
    except WorkflowRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workflows/{run_id}/replay-from-step")
async def replay_workflow_from_step(
    run_id: uuid.UUID,
    payload: WorkflowReplayFromStepRequest,
    tenant_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
):
    """Rewind target/downstream journal rows; normal resume performs execution."""
    del current_user
    try:
        return await WorkflowOpsService().replay_from_step(
            run_id,
            tenant_id=tenant_id,
            step_id=payload.step_id,
            reason=payload.reason,
        )
    except WorkflowOpsConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkflowRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/autonomous-audit")
async def get_autonomous_audit(
    tenant_id: uuid.UUID | None = Query(default=None),
    agent_id: uuid.UUID | None = Query(default=None),
    lookback_hours: int = Query(default=24, ge=1, le=720),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Return a read-only audit of autonomous trigger/self-evolution continuity."""
    del current_user
    await _pin_admin_tenant_scope(db, tenant_id)
    return await build_autonomous_audit_report(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        lookback_hours=lookback_hours,
    )


@router.get("/harness-validation")
async def get_harness_validation(
    tenant_id: uuid.UUID | None = Query(default=None),
    agent_id: uuid.UUID | None = Query(default=None),
    lookback_hours: int = Query(default=168, ge=1, le=2160),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Return a read-only production report for H4/H5 harness evidence."""
    del current_user
    await _pin_admin_tenant_scope(db, tenant_id)
    return await build_harness_validation_report(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        lookback_hours=lookback_hours,
    )


@router.post("/harness-canary/run")
async def post_harness_canary_run(
    payload: HarnessCanaryRunRequest | None = None,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Write explicit H4/H5 canary evidence for autonomous harness verification."""
    del current_user
    payload = payload or HarnessCanaryRunRequest()
    await _pin_admin_tenant_scope(db, payload.tenant_id)
    return await run_harness_canary(
        db=db,
        tenant_id=payload.tenant_id,
        agent_id=payload.agent_id,
        include_h4=payload.include_h4,
        include_h5=payload.include_h5,
        max_agents=payload.max_agents,
        force=payload.force,
    )


@router.post("/plan-mode/cutover")
async def post_plan_mode_cutover(
    payload: PlanModeCutoverRequest | None = None,
    current_user: User = Depends(require_role("platform_admin")),
):
    """Grandfather pre-existing enabled triggers for the Plan Mode cutover (§9.0).

    Stamps ``config.metadata.plan_exempt_reason="preexisting_before_cutover"`` on
    every currently-enabled trigger that has no confirmed plan and no exemption
    yet, so the fail-closed Plan Mode preflight does not quarantine automation
    created before Plan Mode existed. Idempotent and surgical.

    The cutover service opens its own session against the production DB, so this
    is run from inside the container (the internal DB is not reachable off-box).

    Pass ``dry_run=true`` (default) to count what would be stamped without
    writing; ``dry_run=false`` commits. Optionally scope to a single ``agent_id``.
    """
    del current_user
    payload = payload or PlanModeCutoverRequest()
    report = await mark_existing_triggers_plan_exempt(
        agent_id=payload.agent_id,
        commit=not payload.dry_run,
    )
    return {**report, "dry_run": payload.dry_run}


@router.put("/companies/{company_id}/toggle")
async def toggle_company(
    company_id: uuid.UUID,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable a company."""
    result = await db.execute(select(Tenant).where(Tenant.id == company_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    await _pin_admin_tenant_scope(db, company_id)

    new_state = not tenant.is_active
    tenant.is_active = new_state

    # When disabling: pause all running agents
    if not new_state:
        agents = await db.execute(
            select(Agent).where(Agent.tenant_id == company_id, Agent.status == "running")
        )
        for agent in agents.scalars().all():
            agent.status = "paused"

    await db.flush()
    return {"ok": True, "is_active": new_state}


# ─── Platform Settings ─────────────────────────────────

@router.get("/platform-settings", response_model=PlatformSettingsOut)
async def get_platform_settings(
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get platform-level settings."""
    settings: dict[str, bool] = {}

    for key, default in [
        ("allow_self_create_company", True),
        ("invitation_code_enabled", False),
    ]:
        r = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        s = r.scalar_one_or_none()
        settings[key] = s.value.get("enabled", default) if s else default

    return PlatformSettingsOut(**settings)


@router.put("/platform-settings", response_model=PlatformSettingsOut)
async def update_platform_settings(
    data: PlatformSettingsUpdate,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update platform-level settings."""
    updates = data.model_dump(exclude_unset=True)

    for key, value in updates.items():
        r = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        s = r.scalar_one_or_none()
        if s:
            s.value = {"enabled": value}
        else:
            db.add(SystemSetting(key=key, value={"enabled": value}))

    await db.flush()
    return await get_platform_settings(current_user=current_user, db=db)


# ─── Metrics ──────────────────────────────────────────────


class TimeseriesPoint(BaseModel):
    date: str
    total_companies: int = 0
    new_companies: int = 0
    total_users: int = 0
    new_users: int = 0
    total_tokens: int = 0
    new_tokens: int = 0


class LeaderboardEntry(BaseModel):
    name: str
    tokens: int = 0
    company: str | None = None


class MetricsLeaderboard(BaseModel):
    top_companies: list[LeaderboardEntry]
    top_agents: list[LeaderboardEntry]


_MAX_TIMESERIES_DAYS = 90


@router.get("/metrics/timeseries", response_model=list[TimeseriesPoint])
async def get_metrics_timeseries(
    start_date: datetime = Query(...),
    end_date: datetime = Query(...),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Daily time series for companies and users.

    Accepts ISO datetime (e.g. 2026-04-01T00:00:00Z) or date (2026-04-01).
    Token time series is sourced from append-only token_usage_events, not agent
    creation-date proxies.
    """
    start = start_date.date() if isinstance(start_date, datetime) else start_date
    end = end_date.date() if isinstance(end_date, datetime) else end_date
    if start > end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    if (end - start).days > _MAX_TIMESERIES_DAYS:
        raise HTTPException(status_code=422, detail=f"Date range must not exceed {_MAX_TIMESERIES_DAYS} days")

    async with enter_rls_bypass(
        db,
        reason="platform admin metrics timeseries cross-tenant aggregation",
        actor_id=str(current_user.id),
    ) as bypass_db:
        # Daily new counts via SQL aggregation
        new_co_rows = await bypass_db.execute(
            select(
                sqla_func.date(Tenant.created_at).label("d"),
                sqla_func.count().label("cnt"),
            )
            .where(sqla_func.date(Tenant.created_at).between(start, end))
            .group_by(sqla_func.date(Tenant.created_at))
        )
        new_companies: dict[str, int] = {str(r.d): r.cnt for r in new_co_rows}

        new_usr_rows = await bypass_db.execute(
            select(
                sqla_func.date(User.created_at).label("d"),
                sqla_func.count().label("cnt"),
            )
            .where(sqla_func.date(User.created_at).between(start, end))
            .group_by(sqla_func.date(User.created_at))
        )
        new_users_map: dict[str, int] = {str(r.d): r.cnt for r in new_usr_rows}

        # Token usage by actual usage event date.
        new_tok_rows = await bypass_db.execute(
            select(
                sqla_func.date(TokenUsageEvent.created_at).label("d"),
                sqla_func.coalesce(sqla_func.sum(TokenUsageEvent.tokens), 0).label("tokens"),
            )
            .where(sqla_func.date(TokenUsageEvent.created_at).between(start, end))
            .group_by(sqla_func.date(TokenUsageEvent.created_at))
        )
        new_tokens_map: dict[str, int] = {str(r.d): int(r.tokens) for r in new_tok_rows}

        # Cumulative bases before start
        pre_co = await bypass_db.execute(
            select(sqla_func.count()).select_from(Tenant).where(sqla_func.date(Tenant.created_at) < start)
        )
        cum_companies = pre_co.scalar() or 0

        pre_usr = await bypass_db.execute(
            select(sqla_func.count()).select_from(User).where(sqla_func.date(User.created_at) < start)
        )
        cum_users = pre_usr.scalar() or 0

        pre_tok = await bypass_db.execute(
            select(sqla_func.coalesce(sqla_func.sum(TokenUsageEvent.tokens), 0))
            .where(sqla_func.date(TokenUsageEvent.created_at) < start)
        )
        cum_tokens = int(pre_tok.scalar() or 0)

    # Build series
    result: list[TimeseriesPoint] = []
    current = start
    while current <= end:
        date_str = current.isoformat()
        nc = new_companies.get(date_str, 0)
        nu = new_users_map.get(date_str, 0)
        nt = new_tokens_map.get(date_str, 0)
        cum_companies += nc
        cum_users += nu
        cum_tokens += nt
        result.append(TimeseriesPoint(
            date=date_str,
            total_companies=cum_companies,
            new_companies=nc,
            total_users=cum_users,
            new_users=nu,
            total_tokens=cum_tokens,
            new_tokens=nt,
        ))
        current += timedelta(days=1)

    return result


@router.get("/metrics/leaderboards", response_model=MetricsLeaderboard)
async def get_metrics_leaderboards(
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Top 20 companies and agents by token usage."""
    async with enter_rls_bypass(
        db,
        reason="platform admin metrics leaderboard cross-tenant aggregation",
        actor_id=str(current_user.id),
    ) as bypass_db:
        # Top 20 companies
        top_co = await bypass_db.execute(
            select(
                Tenant.name,
                sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0).label("tokens"),
            )
            .join(Agent, Agent.tenant_id == Tenant.id, isouter=True)
            .group_by(Tenant.id, Tenant.name)
            .order_by(sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0).desc())
            .limit(20)
        )

        # Top 20 agents
        agent_tokens = sqla_func.coalesce(Agent.tokens_used_total, 0)
        top_ag = await bypass_db.execute(
            select(
                Agent.name,
                Tenant.name.label("company"),
                agent_tokens.label("tokens"),
            )
            .join(Tenant, Tenant.id == Agent.tenant_id, isouter=True)
            .order_by(agent_tokens.desc())
            .limit(20)
        )

    return MetricsLeaderboard(
        top_companies=[LeaderboardEntry(name=r.name, tokens=r.tokens) for r in top_co],
        top_agents=[LeaderboardEntry(name=r.name, company=r.company or "", tokens=r.tokens) for r in top_ag],
    )


# ─── Legacy T0 → T2 backfill (PR-4) ───────────────────────


@router.get("/agents/{agent_id}/extraction-audit")
async def audit_agent_extraction(
    agent_id: uuid.UUID,
    days: int = Query(7, ge=1, le=30),
    _admin: User = Depends(require_role("platform_admin")),
) -> dict:
    """Report which legacy behavior T0 sessions are missing from T2 backfill."""
    from app.services.extract_agent import audit_extraction_completeness

    return await audit_extraction_completeness(agent_id, days=days)


@router.post("/agents/{agent_id}/backfill-t2")
async def backfill_agent_t2(
    agent_id: uuid.UUID,
    days: int = Query(7, ge=1, le=30),
    dry_run: bool = Query(False),
    _admin: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Replay missing legacy behavior T0 sessions into T2 learnings.

    Pass dry_run=true to preview without writing.
    """
    from app.services.extract_agent import backfill_missing_extractions

    agent_row = await _load_platform_admin_agent_and_pin(
        db,
        agent_id=agent_id,
        admin_user=_admin,
        reason=f"platform admin legacy T0 to T2 backfill lookup for agent {agent_id}",
    )

    return await backfill_missing_extractions(
        agent_id,
        days=days,
        dry_run=dry_run,
        tenant_id=agent_row.tenant_id,
        agent_name=agent_row.name or "Agent",
    )


@router.post("/agents/{agent_id}/backfill-prose")
async def backfill_agent_prose(
    agent_id: uuid.UUID,
    dry_run: bool = Query(True),
    _admin: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """D2: strip inline metadata from this agent's T3 `.md` files down to clean
    `[date][entry_id] content`, migrating sensitivity/status/version + telemetry
    into the lifecycle sidecar and archiving the originals.

    Defaults to ``dry_run=true`` — it touches the production data volume, so the
    owner previews the diff first (safety gate, not an MVP stage).
    """


    await _load_platform_admin_agent_and_pin(
        db,
        agent_id=agent_id,
        admin_user=_admin,
        reason=f"platform admin T3 prose backfill lookup for agent {agent_id}",
    )

    del dry_run  # accepted for API compatibility; nothing runs anymore
    return {"status": "retired", "reason": "legacy flat-T3 prose backfill retired at the C7 two-plane cutover"}
