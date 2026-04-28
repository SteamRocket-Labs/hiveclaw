"""Platform Admin company management API.

Provides endpoints for platform admins to manage companies, view stats,
and control platform-level settings.
"""

import logging
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func as sqla_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_role
from app.database import get_db
from app.models.agent import Agent
from app.models.invitation_code import InvitationCode
from app.models.system_settings import SystemSetting
from app.models.tenant import Tenant
from app.models.user import User
from app.services.autonomous_audit import build_autonomous_audit_report
from app.services.autonomy_repair_plan import apply_autonomy_repair_plan, build_autonomy_repair_plan
from app.services.harness_canary import run_harness_canary
from app.services.harness_validation_report import build_harness_validation_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Module-level strong references to delayed-close tasks so asyncio.create_task's
# weak-ref policy doesn't GC them before the 30s drain window completes.
_pending_delayed_closes: set = set()


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


class AutonomyRepairApplyRequest(BaseModel):
    action_ids: list[str] | None = Field(
        default=None,
        description="Optional stable repair action IDs to apply. Omit to apply all auto-applyable actions under max_risk.",
    )
    max_risk: str = Field(default="medium", pattern="^(low|medium|high)$")


class HarnessCanaryRunRequest(BaseModel):
    tenant_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    include_h4: bool = True
    include_h5: bool = True
    max_agents: int = Field(default=50, ge=1, le=500)
    force: bool = False


# ─── Company Management ────────────────────────────────

@router.get("/companies", response_model=list[CompanyStats])
async def list_companies(
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all companies with stats."""
    tenants = await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
    result = []

    for tenant in tenants.scalars().all():
        tid = tenant.id

        # User count
        uc = await db.execute(
            select(sqla_func.count()).select_from(User).where(User.tenant_id == tid)
        )
        user_count = uc.scalar() or 0

        # Agent count
        ac = await db.execute(
            select(sqla_func.count()).select_from(Agent).where(Agent.tenant_id == tid)
        )
        agent_count = ac.scalar() or 0

        # Running agents
        rc = await db.execute(
            select(sqla_func.count()).select_from(Agent).where(
                Agent.tenant_id == tid, Agent.status == "running"
            )
        )
        agent_running = rc.scalar() or 0

        # Total tokens
        tc = await db.execute(
            select(sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0)).where(
                Agent.tenant_id == tid
            )
        )
        total_tokens = tc.scalar() or 0

        result.append(CompanyStats(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            is_active=tenant.is_active,
            created_at=tenant.created_at,
            user_count=user_count,
            agent_count=agent_count,
            agent_running_count=agent_running,
            total_tokens=total_tokens,
        ))

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


_ALLOWED_MEMORY_BACKENDS = {"md", "hindsight", None}


class MemoryBackendUpdate(BaseModel):
    memory_backend: str | None = Field(
        default=None,
        description="One of 'md', 'hindsight', or null (unset → env fallback).",
    )


@router.patch("/companies/{company_id}/memory-backend")
async def set_company_memory_backend(
    company_id: uuid.UUID,
    payload: MemoryBackendUpdate,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Flip a tenant's memory_backend preference.

    Valid values:
    - "md"        — force MDBackend (T3 MD + BM25)
    - "hindsight" — force HindsightBackend (requires HINDSIGHT_ENABLED=true + URL)
    - null        — tenant-level override unset; env MEMORY_BACKEND applies

    After switching to hindsight, it's recommended to run:
        python -m app.admin.rebuild_hindsight --tenant-id <company_id>
    to backfill the bank from existing T3 markdown.
    """
    del current_user
    if payload.memory_backend not in _ALLOWED_MEMORY_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"memory_backend must be one of {sorted(x or 'null' for x in _ALLOWED_MEMORY_BACKENDS)}",
        )

    result = await db.execute(select(Tenant).where(Tenant.id == company_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    tenant.memory_backend = payload.memory_backend
    await db.flush()

    # Drop cached backend instance so next request builds a fresh one for the
    # new choice. We deliberately DO NOT close the httpx client inline — other
    # coroutines (search/retain_batch) may still hold a reference mid-flight
    # and closing would crash their in-flight requests. Instead, schedule a
    # delayed close so those in-flight calls have time to complete (timeout
    # ceiling in HindsightBackend is 10s; 30s gives ample headroom).
    from app.memory.backend import _backend_cache
    cache_key = f"hindsight:{company_id.hex}"
    stale = _backend_cache.pop(cache_key, None)
    if stale is not None:
        close = getattr(stale, "close", None)
        if close is not None:
            async def _delayed_close(backend):
                import asyncio
                await asyncio.sleep(30)
                try:
                    await backend.close()
                except Exception as exc:
                    logger.warning(
                        "[admin] delayed close of stale backend for %s failed: %s",
                        company_id, exc,
                    )
            import asyncio
            task = asyncio.create_task(_delayed_close(stale))
            _pending_delayed_closes.add(task)
            task.add_done_callback(_pending_delayed_closes.discard)

    return {"ok": True, "memory_backend": tenant.memory_backend}


@router.get("/metrics/memory")
async def get_memory_backend_metrics(
    current_user: User = Depends(require_role("platform_admin")),
):
    """Return in-memory memory-backend counters (recall latency, errors, sync lag)."""
    del current_user
    from app.memory.metrics import snapshot
    return snapshot()


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
    return await build_harness_validation_report(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        lookback_hours=lookback_hours,
    )


@router.get("/autonomy-repair-plan")
async def get_autonomy_repair_plan(
    tenant_id: uuid.UUID | None = Query(default=None),
    agent_id: uuid.UUID | None = Query(default=None),
    lookback_hours: int = Query(default=24, ge=1, le=720),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Return a read-only dry-run repair plan for autonomous audit findings."""
    del current_user
    return await build_autonomy_repair_plan(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        lookback_hours=lookback_hours,
    )


@router.post("/autonomy-repair-plan/apply")
async def post_autonomy_repair_plan_apply(
    payload: AutonomyRepairApplyRequest | None = None,
    tenant_id: uuid.UUID | None = Query(default=None),
    agent_id: uuid.UUID | None = Query(default=None),
    lookback_hours: int = Query(default=24, ge=1, le=720),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Apply auto-applyable autonomy repair actions with precondition checks."""
    del current_user
    payload = payload or AutonomyRepairApplyRequest()
    return await apply_autonomy_repair_plan(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        lookback_hours=lookback_hours,
        action_ids=payload.action_ids,
        max_risk=payload.max_risk,
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
    return await run_harness_canary(
        db=db,
        tenant_id=payload.tenant_id,
        agent_id=payload.agent_id,
        include_h4=payload.include_h4,
        include_h5=payload.include_h5,
        max_agents=payload.max_agents,
        force=payload.force,
    )


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
    Token time series requires a daily usage log table (not yet implemented),
    so token fields return 0 for now.
    """
    start = start_date.date() if isinstance(start_date, datetime) else start_date
    end = end_date.date() if isinstance(end_date, datetime) else end_date
    if start > end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    if (end - start).days > _MAX_TIMESERIES_DAYS:
        raise HTTPException(status_code=422, detail=f"Date range must not exceed {_MAX_TIMESERIES_DAYS} days")

    # Daily new counts via SQL aggregation
    new_co_rows = await db.execute(
        select(
            sqla_func.date(Tenant.created_at).label("d"),
            sqla_func.count().label("cnt"),
        )
        .where(sqla_func.date(Tenant.created_at).between(start, end))
        .group_by(sqla_func.date(Tenant.created_at))
    )
    new_companies: dict[str, int] = {str(r.d): r.cnt for r in new_co_rows}

    new_usr_rows = await db.execute(
        select(
            sqla_func.date(User.created_at).label("d"),
            sqla_func.count().label("cnt"),
        )
        .where(sqla_func.date(User.created_at).between(start, end))
        .group_by(sqla_func.date(User.created_at))
    )
    new_users_map: dict[str, int] = {str(r.d): r.cnt for r in new_usr_rows}

    # Token usage by agent creation date (proxy — no daily log table yet)
    new_tok_rows = await db.execute(
        select(
            sqla_func.date(Agent.created_at).label("d"),
            sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0).label("tokens"),
        )
        .where(sqla_func.date(Agent.created_at).between(start, end))
        .group_by(sqla_func.date(Agent.created_at))
    )
    new_tokens_map: dict[str, int] = {str(r.d): int(r.tokens) for r in new_tok_rows}

    # Cumulative bases before start
    pre_co = await db.execute(
        select(sqla_func.count()).select_from(Tenant).where(sqla_func.date(Tenant.created_at) < start)
    )
    cum_companies = pre_co.scalar() or 0

    pre_usr = await db.execute(
        select(sqla_func.count()).select_from(User).where(sqla_func.date(User.created_at) < start)
    )
    cum_users = pre_usr.scalar() or 0

    pre_tok = await db.execute(
        select(sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0))
        .where(sqla_func.date(Agent.created_at) < start)
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
    # Top 20 companies
    top_co = await db.execute(
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
    top_ag = await db.execute(
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


# ─── T0 → T2 backfill (PR-4) ─────────────────────────────


@router.get("/agents/{agent_id}/extraction-audit")
async def audit_agent_extraction(
    agent_id: uuid.UUID,
    days: int = Query(7, ge=1, le=30),
    _admin: User = Depends(require_role("admin")),
) -> dict:
    """Report which behavior T0 sessions are missing from T2 backfill."""
    from app.services.extract_agent import audit_extraction_completeness

    return await audit_extraction_completeness(agent_id, days=days)


@router.post("/agents/{agent_id}/backfill-t2")
async def backfill_agent_t2(
    agent_id: uuid.UUID,
    days: int = Query(7, ge=1, le=30),
    dry_run: bool = Query(False),
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Replay missing behavior T0 sessions into T2 learnings.

    Pass dry_run=true to preview without writing.
    """
    from app.services.extract_agent import backfill_missing_extractions

    agent_row = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if not agent_row:
        raise HTTPException(status_code=404, detail="agent not found")

    return await backfill_missing_extractions(
        agent_id,
        days=days,
        dry_run=dry_run,
        tenant_id=agent_row.tenant_id,
        agent_name=agent_row.name or "Agent",
    )
