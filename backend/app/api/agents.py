"""Agent (Digital Employee) API routes."""

from collections.abc import Mapping
import logging
import shutil
import uuid
from datetime import datetime, timezone
import inspect
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access, effective_agent_owner_id, require_agent_manage_access
from app.core.security import get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.domain.agent_lifecycle import InvalidTransitionError, TransitionContext, transition
from app.models.agent import Agent, AgentPermission
from app.models.user import User
from app.schemas.schemas import AgentCreate, AgentOut, AgentUpdate
from app.services.agent_identity_lifecycle import (
    agent_lifecycle_active_clause,
    ensure_agent_identity,
    soft_delete_agent,
)
from app.runtime.hooks import hook_registry
from app.services.heartbeat_policy import apply_managed_heartbeat_fields, normalize_agent_heartbeat_output
from app.services.command_registry import build_default_command_registry
from app.services.enterprise_approval_visibility import enterprise_visible_approval_filter
from app.services.mcp_server_service import get_agent_mcp_servers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

AGENT_LIST_ROLE_DESCRIPTION_CHARS = 320


async def _lazy_reset_token_counters(agent: Agent, db: AsyncSession) -> bool:
    """Reset daily/monthly token counters if the day or month has changed.

    Returns True if any counter was reset (caller should commit/flush).
    """
    from datetime import datetime, timezone as tz

    now = datetime.now(tz.utc)
    changed = False

    last_daily = agent.last_daily_reset
    if last_daily is None or last_daily.date() < now.date():
        agent.tokens_used_today = 0
        agent.last_daily_reset = now
        changed = True

    last_monthly = agent.last_monthly_reset
    if last_monthly is None or (last_monthly.year, last_monthly.month) < (now.year, now.month):
        agent.tokens_used_month = 0
        agent.last_monthly_reset = now
        changed = True

    return changed


def _agent_out(agent: Agent) -> AgentOut:
    out = AgentOut.model_validate(agent)
    for field, value in normalize_agent_heartbeat_output({}).items():
        setattr(out, field, value)
    return out


def _agent_out_dict(agent: Agent) -> dict:
    return normalize_agent_heartbeat_output(_agent_out(agent).model_dump())


def _agent_list_summary_stmt():
    return select(
        Agent.id.label("id"),
        Agent.name.label("name"),
        Agent.avatar_url.label("avatar_url"),
        func.left(func.coalesce(Agent.role_description, ""), AGENT_LIST_ROLE_DESCRIPTION_CHARS).label(
            "role_description"
        ),
        Agent.status.label("status"),
        Agent.creator_id.label("creator_id"),
        Agent.sponsor_user_id.label("sponsor_user_id"),
        Agent.participant_id.label("participant_id"),
        Agent.owner_user_id.label("owner_user_id"),
        Agent.tenant_id.label("tenant_id"),
        Agent.primary_model_id.label("primary_model_id"),
        Agent.fallback_model_id.label("fallback_model_id"),
        func.coalesce(Agent.tokens_used_today, 0).label("tokens_used_today"),
        func.coalesce(Agent.tokens_used_month, 0).label("tokens_used_month"),
        func.coalesce(Agent.tokens_used_total, 0).label("tokens_used_total"),
        func.coalesce(Agent.max_tool_rounds, 200).label("max_tool_rounds"),
        func.coalesce(Agent.max_triggers, 20).label("max_triggers"),
        func.coalesce(Agent.min_poll_interval_min, 5).label("min_poll_interval_min"),
        func.coalesce(Agent.webhook_rate_limit, 5).label("webhook_rate_limit"),
        Agent.last_heartbeat_at.label("last_heartbeat_at"),
        Agent.timezone.label("timezone"),
        Agent.agent_type.label("agent_type"),
        Agent.agent_class.label("agent_class"),
        Agent.security_zone.label("security_zone"),
        Agent.execution_mode.label("execution_mode"),
        Agent.created_at.label("created_at"),
        Agent.last_active_at.label("last_active_at"),
        Agent.deleted_at.label("deleted_at"),
        Agent.deactivated_at.label("deactivated_at"),
        Agent.deactivation_reason.label("deactivation_reason"),
    )


def _agent_action_capabilities(access_level: str, *, is_owner: bool) -> dict[str, bool]:
    can_manage = access_level == "manage"
    return {
        "can_use": access_level in {"use", "manage"},
        "can_manage": can_manage,
        "can_manage_schedule": can_manage,
        "can_manage_channel": can_manage,
        "can_manage_permissions": can_manage,
        "can_transfer_ownership": can_manage and is_owner,
    }


def _agent_list_out_from_mapping(
    row: Mapping[str, object],
    *,
    access_level: str,
    current_user_id: uuid.UUID,
) -> AgentOut:
    role_description = str(row.get("role_description") or "")
    if len(role_description) > AGENT_LIST_ROLE_DESCRIPTION_CHARS:
        role_description = role_description[: AGENT_LIST_ROLE_DESCRIPTION_CHARS - 3].rstrip() + "..."

    data = dict(row)
    data["role_description"] = role_description
    data["bio"] = None
    data["welcome_message"] = None
    data["creator_username"] = None
    data["smart_model_routing"] = None
    effective_owner = data.get("owner_user_id") or data.get("creator_id")
    is_owner = str(effective_owner) == str(current_user_id)
    data["access_level"] = access_level
    data["is_owner"] = is_owner
    data["action_capabilities"] = _agent_action_capabilities(access_level, is_owner=is_owner)
    return AgentOut.model_validate(normalize_agent_heartbeat_output(data))


@router.get("/", response_model=list[AgentOut])
async def list_agents(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all agents the current user has access to."""
    # platform_admin & org_admin see all agents (optionally filtered by tenant)
    if current_user.role in ("platform_admin", "org_admin"):
        target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
        stmt = _agent_list_summary_stmt().where(
            Agent.tenant_id == target_tenant_id,
            Agent.agent_class != "internal_system",
            agent_lifecycle_active_clause(),
        )
        result = await db.execute(stmt.order_by(Agent.created_at.desc()))
        return [
            _agent_list_out_from_mapping(row, access_level="manage", current_user_id=current_user.id)
            for row in result.mappings().all()
        ]

    # All users see their currently owned agents + permitted agents. Creator is
    # only the fallback for legacy rows that have not yet been backfilled.
    # All scoped to user's tenant
    user_tenant = current_user.tenant_id

    owned_ids = select(Agent.id).where(
        (Agent.owner_user_id == current_user.id)
        | ((Agent.owner_user_id.is_(None)) & (Agent.creator_id == current_user.id)),
        Agent.tenant_id == user_tenant,
        Agent.agent_class != "internal_system",
        agent_lifecycle_active_clause(),
    )

    # Get agents user has permission to (within their tenant)
    permitted_ids = select(AgentPermission.agent_id).where(
        (AgentPermission.scope_type == "company")
        | ((AgentPermission.scope_type == "user") & (AgentPermission.scope_id == current_user.id))
        | ((AgentPermission.scope_type == "department") & (AgentPermission.scope_id == current_user.department_id))
    )
    # Union
    combined = union_all(owned_ids, permitted_ids).subquery()
    result = await db.execute(
        _agent_list_summary_stmt()
        .where(
            Agent.id.in_(select(combined.c.id)),
            Agent.tenant_id == user_tenant,
            Agent.agent_class != "internal_system",
            agent_lifecycle_active_clause(),
        )
        .order_by(Agent.created_at.desc())
    )
    rows = result.mappings().all()
    agent_ids = [row["id"] for row in rows]
    permission_map: dict[uuid.UUID, str] = {}
    if agent_ids:
        permission_result = await db.execute(
            select(AgentPermission).where(AgentPermission.agent_id.in_(agent_ids))
        )
        for permission in permission_result.scalars().all():
            applies = (
                permission.scope_type == "company"
                or (permission.scope_type == "user" and permission.scope_id == current_user.id)
                or (
                    permission.scope_type == "department"
                    and current_user.department_id
                    and permission.scope_id == current_user.department_id
                )
            )
            if applies and (
                permission_map.get(permission.agent_id) != "manage" or permission.access_level == "manage"
            ):
                permission_map[permission.agent_id] = permission.access_level or "use"

    output = []
    for row in rows:
        effective_owner = row["owner_user_id"] or row["creator_id"]
        access_level = "manage" if effective_owner == current_user.id else permission_map.get(row["id"], "use")
        output.append(
            _agent_list_out_from_mapping(
                row,
                access_level=access_level,
                current_user_id=current_user.id,
            )
        )
    return output


HR_AGENT_NAME = "__system_hr__"
HR_TEMPLATE_VERSION = "hr-flow-v5-personal-kb-work-routing-2026-07-08"


async def _get_existing_hr_agent(db: AsyncSession, tenant_id: uuid.UUID, *, lock_rows: bool) -> Agent | None:
    stmt = (
        select(Agent)
        .where(
            Agent.tenant_id == tenant_id,
            Agent.agent_class == "internal_system",
            Agent.name == HR_AGENT_NAME,
        )
        .order_by(Agent.created_at.asc(), Agent.id.asc())
    )
    if lock_rows:
        stmt = stmt.with_for_update()

    result = await db.execute(stmt)
    hr_agents = list(result.scalars().all())
    if len(hr_agents) > 1:
        logger.error(
            "[HR] Duplicate system HR agents found for tenant %s; using canonical agent %s and ignoring %s duplicate(s)",
            tenant_id,
            hr_agents[0].id,
            len(hr_agents) - 1,
        )
    return hr_agents[0] if hr_agents else None


def _hr_template_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "hr_agent_template"


def _copy_hr_identity_template(src: Path, dest: Path, *, force: bool) -> None:
    if not src.exists():
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        current = dest.read_text(encoding="utf-8")
        incoming = src.read_text(encoding="utf-8")
        if current == incoming:
            return
        if not force:
            return
        backup = dest.with_name(f".{dest.name}.pre-hr-template.bak")
        shutil.copy2(str(dest), str(backup))
        dest.write_text(incoming, encoding="utf-8")
        return

    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _retire_hr_template_skill(agent_dir: Path, folder_name: str) -> None:
    """Move retired bundled HR skills out of the active skill path."""
    skill_dir = agent_dir / "skills" / folder_name
    if not skill_dir.exists():
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    retired_root = agent_dir / "skills" / ".retired" / folder_name
    retired_dir = retired_root / stamp
    counter = 1
    while retired_dir.exists():
        counter += 1
        retired_dir = retired_root / f"{stamp}-{counter}"

    retired_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill_dir), str(retired_dir))


def _sync_hr_agent_workspace(agent_dir: Path, *, force_identity_files: bool) -> None:
    """Sync the system HR workspace with the bundled template contract."""
    hr_template_dir = _hr_template_dir()
    if not hr_template_dir.exists():
        return

    agent_dir.mkdir(parents=True, exist_ok=True)

    for fname in ("soul.md", "HEARTBEAT.md"):
        _copy_hr_identity_template(
            hr_template_dir / fname,
            agent_dir / fname,
            force=force_identity_files,
        )

    hr_skills = hr_template_dir / "skills"
    if hr_skills.exists():
        from app.services.skill_installation import collect_skill_package_files, install_active_skill_package

        for skill_dir in sorted(path for path in hr_skills.iterdir() if path.is_dir()):
            if not (skill_dir / "SKILL.md").is_file():
                continue
            install_active_skill_package(
                workspace=agent_dir,
                folder_name=skill_dir.name,
                files=collect_skill_package_files(skill_dir),
                source="hr_template",
                overwrite=True,
            )

    _retire_hr_template_skill(agent_dir, "hr-guide")
    (agent_dir / ".hr_template_version").write_text(HR_TEMPLATE_VERSION, encoding="utf-8")


@router.get("/system/hr")
async def get_or_create_hr_agent(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the HR onboarding agent for the current tenant. Creates one if it doesn't exist."""
    tenant_id = current_user.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant")

    # Look up existing HR agent with a row lock to prevent duplicate creation.
    # If legacy duplicates exist, choose the oldest row deterministically rather
    # than turning the workspace HR page into a 500.
    hr_agent = await _get_existing_hr_agent(db, tenant_id, lock_rows=True)

    # Shared: find default model for tenant
    from app.models.llm import LLMModel

    async def _get_default_model() -> LLMModel | None:
        result = await db.execute(
            select(LLMModel)
            .where(LLMModel.tenant_id == tenant_id, LLMModel.enabled.is_(True))
            .order_by(LLMModel.created_at)
            .limit(1)
        )
        return result.scalar_one_or_none()

    if hr_agent:
        # Auto-assign model if the agent was created before any model was configured
        if not hr_agent.primary_model_id:
            default_model = await _get_default_model()
            if default_model:
                hr_agent.primary_model_id = default_model.id
                from app.services.ai_assets import register_agent_asset

                await register_agent_asset(
                    db,
                    hr_agent,
                    change_source="update",
                    actor_user_id=current_user.id,
                    change_message="HR Agent default model assigned",
                )
                await db.commit()

        # Keep existing HR agents on the current hiring-flow contract. Older
        # workspaces are refreshed once per template version with local backups.
        from app.services.agent_manager import agent_manager

        agent_dir = agent_manager._agent_dir(hr_agent.id)
        if not agent_dir.exists():
            await agent_manager.initialize_agent_files(db, hr_agent)
        version_file = agent_dir / ".hr_template_version"
        current_version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
        _sync_hr_agent_workspace(agent_dir, force_identity_files=current_version != HR_TEMPLATE_VERSION)

        return {"id": str(hr_agent.id), "name": hr_agent.name, "status": hr_agent.status}

    # Lazy-create the HR agent (race-safe: unique constraint on name+tenant+agent_class
    # will reject duplicates if two requests slip past the lock)
    from app.services.agent_manager import agent_manager

    default_model = await _get_default_model()

    hr_agent = Agent(
        name=HR_AGENT_NAME,
        role_description="Digital Employee Onboarding Specialist — guides users through creating new digital employees via conversation",
        creator_id=current_user.id,
        sponsor_user_id=current_user.id,
        owner_user_id=current_user.id,
        tenant_id=tenant_id,
        agent_type="native",
        agent_class="internal_system",
        security_zone="standard",
        primary_model_id=default_model.id if default_model else None,
        status="creating",
    )
    db.add(hr_agent)
    await ensure_agent_identity(db, hr_agent, display_name="HR Onboarding Agent", avatar_url=None)

    # No public permissions — HR agent is only accessible via this endpoint
    db.add(
        AgentPermission(
            agent_id=hr_agent.id,
            tenant_id=hr_agent.tenant_id,
            scope_type="company",
            access_level="use",
        )
    )
    await db.flush()

    from app.services.ai_assets import register_agent_asset

    await register_agent_asset(
        db,
        hr_agent,
        change_source="create",
        actor_user_id=current_user.id,
        change_message="HR Agent created",
    )

    # Initialize files using standard template (same as normal agents)
    await agent_manager.initialize_agent_files(db, hr_agent)

    # Overlay HR-specific files (soul.md, HEARTBEAT.md, skills/)
    agent_dir = agent_manager._agent_dir(hr_agent.id)
    _sync_hr_agent_workspace(agent_dir, force_identity_files=True)

    # Start container
    await agent_manager.start_container(db, hr_agent)
    await db.flush()

    try:
        await db.commit()
    except Exception:
        # Race condition: another request created the HR agent first
        await db.rollback()
        existing = await _get_existing_hr_agent(db, tenant_id, lock_rows=False)
        if existing:
            return {"id": str(existing.id), "name": existing.name, "status": existing.status}
        raise HTTPException(status_code=500, detail="Failed to create HR agent")

    logger.info(f"[HR] Created HR agent {hr_agent.id} for tenant {tenant_id}")
    return {"id": str(hr_agent.id), "name": hr_agent.name, "status": hr_agent.status}


async def _validate_model_refs(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    *,
    primary_model_id: uuid.UUID | None,
    fallback_model_id: uuid.UUID | None,
) -> None:
    """Fail loud if an agent points at a model it cannot actually use.

    An agent's primary/fallback model must exist, belong to the agent's tenant,
    and be enabled. A dangling / cross-tenant / disabled reference makes the
    runtime model lookup return None and silently fall back to a different model
    (the Web3 researcher outage: a cross-tenant DeepSeek ref degraded to a 200K
    MiniMax window and thrashed compaction). Reject it at the configuration edge.
    """
    from app.models.llm import LLMModel

    for label, model_id in (
        ("primary_model_id", primary_model_id),
        ("fallback_model_id", fallback_model_id),
    ):
        if model_id is None:
            continue
        result = await db.execute(
            select(LLMModel).where(
                LLMModel.id == model_id,
                LLMModel.tenant_id == tenant_id,
                LLMModel.enabled.is_(True),
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=400,
                detail=(f"{label} 指向的模型不存在、未启用或不属于本公司,请选择本公司已启用的模型"),
            )


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Administrative creation endpoint; standard users create through System HR."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="请通过 HR Agent 创建数字员工；直接创建接口仅供组织管理员使用。",
        )

    # Determine target tenant: normally user's tenant; admins can override via payload
    target_tenant_id = current_user.tenant_id
    if current_user.role in ("platform_admin", "org_admin"):
        target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, data.tenant_id)

    # Get default limits from target tenant
    default_max_triggers = 20
    default_min_poll = 5
    default_webhook_rate = 5
    if target_tenant_id:
        from app.models.tenant import Tenant

        tenant_result = await db.execute(select(Tenant).where(Tenant.id == target_tenant_id))
        tenant = tenant_result.scalar_one_or_none()
        if tenant:
            default_max_triggers = tenant.default_max_triggers or 20
            default_min_poll = tenant.min_poll_interval_floor or 5
            default_webhook_rate = tenant.max_webhook_rate_ceiling or 5

    await _validate_model_refs(
        db,
        target_tenant_id,
        primary_model_id=data.primary_model_id,
        fallback_model_id=data.fallback_model_id,
    )

    agent = Agent(
        name=data.name,
        role_description=data.role_description,
        bio=data.bio,
        avatar_url=data.avatar_url,
        creator_id=current_user.id,
        sponsor_user_id=current_user.id,
        owner_user_id=current_user.id,
        tenant_id=target_tenant_id,
        agent_type="native",
        primary_model_id=data.primary_model_id,
        fallback_model_id=data.fallback_model_id,
        agent_class=data.agent_class,
        security_zone=data.security_zone,
        execution_mode=data.execution_mode,
        smart_model_routing=data.smart_model_routing.model_dump() if data.smart_model_routing else None,
        status="draft",
        max_triggers=default_max_triggers,
        min_poll_interval_min=default_min_poll,
        webhook_rate_limit=default_webhook_rate,
    )
    apply_managed_heartbeat_fields(agent)
    db.add(agent)
    await ensure_agent_identity(db, agent)

    # Lifecycle: draft → creating (validated by state machine)
    try:
        ctx = TransitionContext(is_system=True)
        agent.status = transition("draft", "creating", ctx)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Set permissions
    access_level = data.permission_access_level if data.permission_access_level in ("use", "manage") else "use"
    if data.permission_scope_type == "company":
        db.add(
            AgentPermission(
                agent_id=agent.id, tenant_id=agent.tenant_id, scope_type="company", access_level=access_level
            )
        )
    elif data.permission_scope_type == "user":
        if data.permission_scope_ids:
            for scope_id in data.permission_scope_ids:
                db.add(
                    AgentPermission(
                        agent_id=agent.id,
                        tenant_id=agent.tenant_id,
                        scope_type="user",
                        scope_id=scope_id,
                        access_level=access_level,
                    )
                )
        else:
            # "仅自己" — insert creator as the only permitted user
            db.add(
                AgentPermission(
                    agent_id=agent.id,
                    tenant_id=agent.tenant_id,
                    scope_type="user",
                    scope_id=current_user.id,
                    access_level="manage",
                )
            )

    await db.flush()

    # Assign default platform tools
    from app.services.tool_seeder import assign_default_tools_to_agent

    await assign_default_tools_to_agent(db, agent.id)
    await db.flush()

    # Initialize agent — wrapped in try/except for rollback on failure (C-01)
    try:
        from app.services.agent_manager import agent_manager

        await agent_manager.initialize_agent_files(
            db,
            agent,
            personality=data.personality,
            boundaries=data.boundaries,
        )

        # Copy selected skills + mandatory default skills into agent workspace
        from app.models.skill import Skill
        from app.api.skills import _skill_visible_to_user
        from sqlalchemy.orm import selectinload

        default_result = await db.execute(select(Skill).where(Skill.is_default, Skill.tenant_id.is_(None)))
        default_ids = {s.id for s in default_result.scalars().all()}
        all_skill_ids = set(data.skill_ids or []) | default_ids

        if all_skill_ids:
            agent_dir = agent_manager._agent_dir(agent.id)
            from app.services.skill_installation import install_active_skill_package

            for sid in all_skill_ids:
                result = await db.execute(select(Skill).where(Skill.id == sid).options(selectinload(Skill.files)))
                skill = result.scalar_one_or_none()
                if not skill or not _skill_visible_to_user(skill, current_user):
                    continue
                install_active_skill_package(
                    workspace=agent_dir,
                    folder_name=skill.folder_name,
                    files=[{"path": sf.path, "content": sf.content} for sf in skill.files],
                    source=f"agent_create_registry_skill:{skill.id}",
                    overwrite=True,
                )

        # Start container
        try:
            await agent_manager.start_container(db, agent)
        except Exception as _container_exc:
            logger.warning("[Agent] Container start failed (non-fatal): %s", _container_exc)
        await db.flush()

        # Transition to idle so heartbeat can pick up the agent
        if agent.status == "creating":
            agent.status = "idle"
            await db.flush()

    except Exception as init_err:
        logger.error("[Agent] Creation failed for %s, rolling back: %s", agent.name, init_err)
        agent.status = "error"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Agent creation failed: {type(init_err).__name__}")

    # Audit: agent created
    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="agent.created",
            severity="info",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=current_user.tenant_id,
            action="create_agent",
            resource_type="agent",
            resource_id=agent.id,
            details={"name": agent.name, "agent_type": agent.agent_type},
        )
    except Exception as _audit_err:
        logger.warning("Audit write failed for agent.created: %s", _audit_err)

    from app.services.ai_asset_adapters import project_agent
    from app.services.ai_assets import register_projection

    await register_projection(
        db,
        project_agent(agent),
        change_source="create",
        actor_user_id=current_user.id,
        change_message="Agent created",
    )
    return _agent_out(agent)


@router.get("/{agent_id}/channel-capabilities")
async def get_agent_channel_capabilities(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the real capability matrix for each configured channel."""
    await check_agent_access(db, current_user, agent_id)
    from app.services.channel_delivery_service import ChannelDeliveryService

    return await ChannelDeliveryService.resolve_agent_capabilities(db=db, agent_id=agent_id)


@router.get("/{agent_id}/a2a-card")
async def get_agent_a2a_card(
    agent_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a machine-readable A2A-style Agent Card for an accessible agent."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    from app.services.interoperability import build_a2a_agent_card

    return build_a2a_agent_card(agent, base_url=str(request.base_url).rstrip("/"))


@router.get("/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent details."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    # Lazy reset token counters
    if await _lazy_reset_token_counters(agent, db):
        await db.commit()
    out = _agent_out_dict(agent)
    out["access_level"] = access_level
    is_owner = str(getattr(agent, "owner_user_id", None) or agent.creator_id) == str(current_user.id)
    out["is_owner"] = is_owner
    out["action_capabilities"] = _agent_action_capabilities(access_level, is_owner=is_owner)

    # Resolve creator + owner usernames (one extra query each, only on detail page)
    if agent.creator_id:
        creator_result = await db.execute(select(User).where(User.id == agent.creator_id))
        creator = creator_result.scalar_one_or_none()
        out["creator_username"] = creator.username if creator else None
    if agent.owner_user_id:
        owner_result = await db.execute(select(User).where(User.id == agent.owner_user_id))
        owner = owner_result.scalar_one_or_none()
        out["owner_username"] = owner.display_name if owner else None

    # Resolve effective timezone (agent → tenant → UTC)
    effective_tz = agent.timezone
    if not effective_tz and agent.tenant_id:
        from app.models.tenant import Tenant

        t_result = await db.execute(select(Tenant).where(Tenant.id == agent.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant:
            effective_tz = tenant.timezone or "UTC"
    out["effective_timezone"] = effective_tz or "UTC"

    return out


@router.get("/{agent_id}/evolution")
async def get_agent_evolution(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read-only view of an agent's self-evolution activity.

    Surfaces Memory/Soul/Skill ecosystem/Skill tuning lanes from the agent
    workspace. Pure read; never mutates agent state. Missing or corrupt files
    degrade to an empty structure.
    """
    await check_agent_access(db, current_user, agent_id)

    from pathlib import Path

    from app.services.agent_evolution_view import build_agent_evolution_view

    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    return build_agent_evolution_view(workspace)


@router.post("/{agent_id}/evolution/soul-candidates/{candidate_id}/approve")
async def approve_agent_soul_candidate(
    agent_id: uuid.UUID,
    candidate_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner-authorized commit of a held soul candidate.

    Approval unlocks only the owner hold — the Platform Soul Gate's physical
    hard checks (base drift, schema, frozen-charter preservation) still run
    and can refuse. Requires manage access; audited with the approver id.
    """
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Soul candidate approval requires manage access")

    from pathlib import Path

    from app.services.soul_approval import approve_soul_candidate

    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    return await approve_soul_candidate(
        workspace=workspace,
        agent_id=agent_id,
        candidate_id=candidate_id,
        approver_id=current_user.id,
    )


@router.post("/{agent_id}/evolution/soul-candidates/{candidate_id}/reject")
async def reject_agent_soul_candidate(
    agent_id: uuid.UUID,
    candidate_id: str,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner rejection of a held soul candidate; soul.md stays untouched."""
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Soul candidate rejection requires manage access")

    from pathlib import Path

    from app.services.soul_approval import reject_soul_candidate

    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    return await reject_soul_candidate(
        workspace=workspace,
        agent_id=agent_id,
        candidate_id=candidate_id,
        approver_id=current_user.id,
        reason=str((payload or {}).get("reason") or ""),
    )


@router.get("/{agent_id}/extension-registry")
async def get_agent_extension_registry(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serve the CCPlus ExtensionRegistryV1 projection for one agent.

    Distinct from the existing skills+MCP ``/{agent_id}/extensions`` endpoint
    (mcp_servers router): this is the unified CCPlus ExtensionRegistryV1 read
    model (skills + deferred tool packs with ToolSpecV1-derived metadata).

    Read-only read model over the agent's installed skills plus the platform
    deferred runtime tool groups. Each tool_pack descriptor carries real
    ToolSpecV1-derived tool metadata (capability bundle, read-only/destructive/
    concurrency flags, deferred-vs-CORE loading, result budget). Pure read; it
    never installs, enables, or mutates extension state.
    """
    from dataclasses import asdict

    from app.services.extension_registry import build_extension_registry_projection
    from app.services.governance_capability_taxonomy import iter_runtime_l2_capabilities
    from app.skills.loader import WorkspaceSkillLoader

    await check_agent_access(db, current_user, agent_id)

    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    try:
        skills = WorkspaceSkillLoader().load_from_workspace(workspace)
    except Exception:
        logger.warning("[Extensions] skill load failed for agent %s", agent_id, exc_info=True)
        skills = []

    tool_packs = [
        {
            "id": descriptor.name,
            "name": descriptor.name,
            "source": descriptor.source,
            "tools": list(descriptor.tools),
            "runtime_effects": [
                "taxonomy:governance_capability",
                f"layer:{descriptor.layer}",
                f"default_enabled:{str(descriptor.default_enabled).lower()}",
            ],
            "audit_refs": list(descriptor.audit_refs or (f"governance_capability://{descriptor.name}",)),
        }
        for descriptor in iter_runtime_l2_capabilities()
    ]

    command_registry = build_default_command_registry(include_optional_coding_pack=True)
    mcp_servers_value = get_agent_mcp_servers(db, agent_id)
    mcp_servers = await mcp_servers_value if inspect.isawaitable(mcp_servers_value) else mcp_servers_value
    projection = build_extension_registry_projection(
        skills=skills,
        hook_catalog=hook_registry.describe_event_catalog(),
        commands=command_registry.values(),
        mcp_servers=mcp_servers,
        tool_packs=tool_packs,
    )
    return asdict(projection)


@router.get("/{agent_id}/capability-installs")
async def get_agent_capability_installs(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List persistent capability install records for one agent."""
    await check_agent_access(db, current_user, agent_id)
    from app.services.capability_install_service import list_capability_installs

    return await list_capability_installs(agent_id=agent_id)


@router.get("/{agent_id}/permissions")
async def get_agent_permissions(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent permission scope."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    perms = result.scalars().all()

    if not perms:
        return {
            "scope_type": "user",
            "scope_ids": [],
            "access_level": access_level,
            "is_owner": effective_agent_owner_id(agent) == current_user.id,
        }

    scope_type = perms[0].scope_type
    scope_ids = [str(p.scope_id) for p in perms if p.scope_id]
    perm_access_level = perms[0].access_level or "use"

    # Resolve names for display
    scope_names = []
    if scope_type == "user":
        for sid in scope_ids:
            r = await db.execute(select(User).where(User.id == uuid.UUID(sid)))
            u = r.scalar_one_or_none()
            if u:
                scope_names.append({"id": sid, "name": u.display_name or u.username})

    return {
        "scope_type": scope_type,
        "scope_ids": scope_ids,
        "scope_names": scope_names,
        "access_level": perm_access_level,
        "is_owner": effective_agent_owner_id(agent) == current_user.id,
    }


@router.put("/{agent_id}/permissions")
async def update_agent_permissions(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent permission scope (owner or admin only)."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    scope_type = data.get("scope_type", "company")
    scope_ids = data.get("scope_ids", [])
    access_level = data.get("access_level", "use")
    if access_level not in ("use", "manage"):
        access_level = "use"

    # Delete existing permissions
    from sqlalchemy import delete as sql_delete

    await db.execute(sql_delete(AgentPermission).where(AgentPermission.agent_id == agent_id))

    # Insert new permissions
    if scope_type == "company":
        db.add(
            AgentPermission(
                agent_id=agent_id, tenant_id=agent.tenant_id, scope_type="company", access_level=access_level
            )
        )
    elif scope_type == "user":
        if scope_ids:
            for sid in scope_ids:
                db.add(
                    AgentPermission(
                        agent_id=agent_id,
                        tenant_id=agent.tenant_id,
                        scope_type="user",
                        scope_id=uuid.UUID(sid),
                        access_level=access_level,
                    )
                )
        else:
            # "仅自己"
            db.add(
                AgentPermission(
                    agent_id=agent_id,
                    tenant_id=agent.tenant_id,
                    scope_type="user",
                    scope_id=current_user.id,
                    access_level="manage",
                )
            )

    await db.commit()
    return {"status": "ok"}


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    data: AgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent settings (creator or admin)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    if _access != "manage":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Manage access is required to update agent settings"
        )

    update_data = data.model_dump(exclude_unset=True)

    clamped_fields = []  # track fields adjusted by tenant floor

    # Enforce trigger limit floors from tenant
    trigger_fields = {"min_poll_interval_min", "webhook_rate_limit", "max_triggers"}
    if trigger_fields & set(update_data.keys()) and current_user.tenant_id:
        from app.models.tenant import Tenant

        t_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant:
            if "min_poll_interval_min" in update_data:
                original = update_data["min_poll_interval_min"]
                update_data["min_poll_interval_min"] = max(original, tenant.min_poll_interval_floor)
                if update_data["min_poll_interval_min"] != original:
                    clamped_fields.append(
                        {
                            "field": "min_poll_interval_min",
                            "requested": original,
                            "applied": update_data["min_poll_interval_min"],
                            "reason": "company_floor",
                        }
                    )
            if "webhook_rate_limit" in update_data:
                original = update_data["webhook_rate_limit"]
                update_data["webhook_rate_limit"] = min(original, tenant.max_webhook_rate_ceiling)
                if update_data["webhook_rate_limit"] != original:
                    clamped_fields.append(
                        {
                            "field": "webhook_rate_limit",
                            "requested": original,
                            "applied": update_data["webhook_rate_limit"],
                            "reason": "company_ceiling",
                        }
                    )

    if "primary_model_id" in update_data or "fallback_model_id" in update_data:
        await _validate_model_refs(
            db,
            agent.tenant_id,
            primary_model_id=update_data.get("primary_model_id"),
            fallback_model_id=update_data.get("fallback_model_id"),
        )

    for field, value in update_data.items():
        setattr(agent, field, value)
    await db.flush()

    # Sync Participant display_name / avatar if changed
    if "name" in update_data or "avatar_url" in update_data:
        from app.models.participant import Participant

        p_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id))
        p = p_r.scalar_one_or_none()
        if p:
            if "name" in update_data:
                p.display_name = agent.name
            if "avatar_url" in update_data:
                p.avatar_url = agent.avatar_url
            await db.flush()

    # Audit: agent updated
    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="agent.updated",
            severity="info",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=current_user.tenant_id,
            action="update_agent",
            resource_type="agent",
            resource_id=agent.id,
            details={"changed_fields": list(update_data.keys())},
        )
    except Exception:
        logger.warning("Audit write failed for agent.updated", exc_info=True)

    from app.services.ai_asset_adapters import project_agent
    from app.services.ai_assets import register_projection

    await register_projection(
        db,
        project_agent(agent),
        change_source="update",
        actor_user_id=current_user.id,
        change_message=f"Agent fields updated: {', '.join(sorted(update_data))}",
    )
    out = _agent_out_dict(agent)
    if clamped_fields:
        out["_clamped_fields"] = clamped_fields
    return out


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a digital employee (admin only)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if current_user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent is an enterprise asset; only an admin can delete it.",
        )

    # Stop container and archive files (best effort)
    from app.services.agent_manager import agent_manager

    try:
        await agent_manager.remove_container(agent)
    except Exception as e:
        logger.warning("Failed to remove container for agent %s: %s", agent_id, e)
    try:
        await agent_manager.archive_agent_files(agent.id)
    except Exception as e:
        logger.warning("Failed to archive files for agent %s: %s", agent_id, e)

    await soft_delete_agent(db, agent, actor_id=current_user.id, reason="delete_agent")

    from app.services.ai_asset_adapters import project_agent
    from app.services.ai_assets import register_projection

    await register_projection(
        db,
        project_agent(agent),
        change_source="revoke",
        actor_user_id=current_user.id,
        change_message="Agent soft-deleted",
    )

    # Audit: agent deleted (before commit so we still have agent data)
    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="agent.deleted",
            severity="warn",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=current_user.tenant_id,
            action="delete_agent",
            resource_type="agent",
            resource_id=agent.id,
            details={"name": agent.name},
        )
    except Exception:
        logger.warning("Audit write failed for agent.deleted", exc_info=True)

    await db.commit()


@router.post("/{agent_id}/start", response_model=AgentOut)
async def start_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start an agent's container."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    from app.services.agent_manager import agent_manager

    await agent_manager.start_container(db, agent)
    await db.flush()

    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="agent.started",
            severity="info",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=current_user.tenant_id,
            action="start_agent",
            resource_type="agent",
            resource_id=agent.id,
            details={"name": agent.name},
        )
    except Exception:
        logger.warning("Audit write failed for agent.started", exc_info=True)

    return _agent_out(agent)


@router.post("/{agent_id}/stop", response_model=AgentOut)
async def stop_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop an agent's container."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    from app.services.agent_manager import agent_manager

    await agent_manager.stop_container(agent)
    await db.flush()

    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="agent.stopped",
            severity="info",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=current_user.tenant_id,
            action="stop_agent",
            resource_type="agent",
            resource_id=agent.id,
            details={"name": agent.name},
        )
    except Exception:
        logger.warning("Audit write failed for agent.stopped", exc_info=True)

    return _agent_out(agent)


# ─── Agent-Level Approvals ──────────────────────────────


@router.get("/{agent_id}/approvals")
async def list_agent_approvals(
    agent_id: uuid.UUID,
    status_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List approval requests for a specific agent. Only creator or admin can view."""
    await require_agent_manage_access(db, current_user, agent_id)

    from app.models.audit import ApprovalRequest

    query = select(ApprovalRequest).where(ApprovalRequest.agent_id == agent_id)
    query = query.where(enterprise_visible_approval_filter(ApprovalRequest))
    if status_filter:
        query = query.where(ApprovalRequest.status == status_filter)
    query = query.order_by(ApprovalRequest.created_at.desc())
    result = await db.execute(query)
    approvals = result.scalars().all()

    return [
        {
            "id": str(a.id),
            "agent_id": str(a.agent_id),
            "action_type": a.action_type,
            "details": a.details,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "resolved_by": str(a.resolved_by) if a.resolved_by else None,
            "decision_id": a.decision_id,
            "tool_name": a.tool_name,
            "normalized_arguments": a.normalized_arguments,
            "input_hash": a.input_hash,
            "policy_snapshot_hash": a.policy_snapshot_hash,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "consumed_at": a.consumed_at.isoformat() if a.consumed_at else None,
            "execution_status": a.execution_status,
            "execution_receipt": a.execution_receipt,
        }
        for a in approvals
    ]


@router.post("/{agent_id}/approvals/{approval_id}/resolve")
async def resolve_agent_approval(
    agent_id: uuid.UUID,
    approval_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject a pending approval for a specific agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    from app.services.approval_service import approval_service

    action = data.get("action", "reject")
    try:
        approval = await approval_service.resolve_approval(db, approval_id, current_user, action)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await db.commit()
    return {
        "id": str(approval.id),
        "status": approval.status,
        "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
    }
