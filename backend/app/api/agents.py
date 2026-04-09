"""Agent (Digital Employee) API routes."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.core.tenant_scope import resolve_tenant_scope
from app.database import get_db
from app.domain.agent_lifecycle import InvalidTransitionError, TransitionContext, transition
from app.models.agent import Agent, AgentPermission
from app.models.user import User
from app.schemas.schemas import AgentCreate, AgentOut, AgentUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


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


@router.get("/", response_model=list[AgentOut])
async def list_agents(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all agents the current user has access to."""
    # platform_admin & org_admin see all agents (optionally filtered by tenant)
    if current_user.role in ("platform_admin", "org_admin"):
        target_tenant_id = resolve_tenant_scope(current_user, tenant_id)
        stmt = select(Agent).where(
            Agent.tenant_id == target_tenant_id,
            Agent.agent_class != "internal_system",
        )
        result = await db.execute(stmt.order_by(Agent.created_at.desc()))
        agents = result.scalars().all()
        # Lazy reset token counters
        needs_flush = False
        for a in agents:
            if await _lazy_reset_token_counters(a, db):
                needs_flush = True
        if needs_flush:
            await db.commit()
        return [AgentOut.model_validate(a) for a in agents]

    # All users see their own created agents + permitted
    # All scoped to user's tenant
    user_tenant = current_user.tenant_id

    # Get agents user created (within their tenant), excluding system agents
    created = select(Agent).where(
        Agent.creator_id == current_user.id,
        Agent.tenant_id == user_tenant,
        Agent.agent_class != "internal_system",
    )

    # Get agents user has permission to (within their tenant)
    permitted_ids = (
        select(AgentPermission.agent_id)
        .where(
            (AgentPermission.scope_type == "company")
            | ((AgentPermission.scope_type == "user") & (AgentPermission.scope_id == current_user.id))
            | (
                (AgentPermission.scope_type == "department")
                & (AgentPermission.scope_id == current_user.department_id)
            )
        )
    )
    permitted = select(Agent).where(
        Agent.id.in_(permitted_ids),
        Agent.tenant_id == user_tenant,
        Agent.agent_class != "internal_system",
    )

    # Union
    from sqlalchemy import union_all

    combined = union_all(created, permitted).subquery()
    result = await db.execute(
        select(Agent).where(Agent.id.in_(select(combined.c.id))).order_by(Agent.created_at.desc())
    )
    agents = result.scalars().all()
    # Lazy reset token counters
    needs_flush = False
    for a in agents:
        if await _lazy_reset_token_counters(a, db):
            needs_flush = True
    if needs_flush:
        await db.commit()
    return [AgentOut.model_validate(a) for a in agents]


HR_AGENT_NAME = "__system_hr__"


@router.get("/system/hr")
async def get_or_create_hr_agent(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the HR onboarding agent for the current tenant. Creates one if it doesn't exist."""
    tenant_id = current_user.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant")

    # Look up existing HR agent (with row lock to prevent duplicate creation)
    result = await db.execute(
        select(Agent).where(
            Agent.tenant_id == tenant_id,
            Agent.agent_class == "internal_system",
            Agent.name == HR_AGENT_NAME,
        ).with_for_update(skip_locked=True)
    )
    hr_agent = result.scalar_one_or_none()

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
                await db.commit()

        # Always sync HR agent workspace with latest template
        # (soul.md, HEARTBEAT.md, focus.md, and skills/ synced to ensure updates propagate)
        from app.services.agent_manager import agent_manager
        import shutil
        from pathlib import Path
        agent_dir = agent_manager._agent_dir(hr_agent.id)
        if not agent_dir.exists():
            await agent_manager.initialize_agent_files(db, hr_agent)
        hr_template_dir = Path(__file__).resolve().parent.parent.parent / "hr_agent_template"
        if hr_template_dir.exists():
            agent_dir.mkdir(parents=True, exist_ok=True)
            # Sync soul.md, HEARTBEAT.md, focus.md from template (create if missing)
            for fname in ("soul.md", "HEARTBEAT.md", "focus.md"):
                src = hr_template_dir / fname
                dest = agent_dir / fname
                if src.exists() and not dest.exists():
                    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            # Sync skills/
            hr_skills = hr_template_dir / "skills"
            if hr_skills.exists():
                for skill_file in hr_skills.rglob("*"):
                    if skill_file.is_file():
                        rel = skill_file.relative_to(hr_skills)
                        dest = agent_dir / "skills" / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(skill_file), str(dest))

        return {"id": str(hr_agent.id), "name": hr_agent.name, "status": hr_agent.status}

    # Lazy-create the HR agent (race-safe: unique constraint on name+tenant+agent_class
    # will reject duplicates if two requests slip past the lock)
    from app.models.participant import Participant
    from app.services.agent_manager import agent_manager

    default_model = await _get_default_model()

    hr_agent = Agent(
        name=HR_AGENT_NAME,
        role_description="Digital Employee Onboarding Specialist — guides users through creating new digital employees via conversation",
        creator_id=current_user.id,
        tenant_id=tenant_id,
        agent_type="native",
        agent_class="internal_system",
        security_zone="standard",
        primary_model_id=default_model.id if default_model else None,
        status="creating",
    )
    db.add(hr_agent)
    await db.flush()

    # Participant identity
    db.add(Participant(
        type="agent", ref_id=hr_agent.id,
        display_name="HR Onboarding Agent", avatar_url=None,
    ))
    await db.flush()

    # No public permissions — HR agent is only accessible via this endpoint
    db.add(AgentPermission(
        agent_id=hr_agent.id, scope_type="company", access_level="use",
    ))
    await db.flush()

    # Initialize files using standard template (same as normal agents)
    await agent_manager.initialize_agent_files(db, hr_agent)

    # Overlay HR-specific files (soul.md, skills/CREATE_EMPLOYEE.md)
    import shutil
    from pathlib import Path
    hr_template_dir = Path(__file__).resolve().parent.parent.parent / "hr_agent_template"
    agent_dir = agent_manager._agent_dir(hr_agent.id)

    if hr_template_dir.exists():
        # Copy HR soul.md (overwrites the standard one)
        hr_soul = hr_template_dir / "soul.md"
        if hr_soul.exists():
            (agent_dir / "soul.md").write_text(hr_soul.read_text(encoding="utf-8"), encoding="utf-8")
        # Copy HR-specific skills
        hr_skills = hr_template_dir / "skills"
        if hr_skills.exists():
            for skill_file in hr_skills.rglob("*"):
                if skill_file.is_file():
                    rel = skill_file.relative_to(hr_skills)
                    dest = agent_dir / "skills" / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(skill_file), str(dest))

    # Start container
    await agent_manager.start_container(db, hr_agent)
    await db.flush()

    try:
        await db.commit()
    except Exception:
        # Race condition: another request created the HR agent first
        await db.rollback()
        retry_result = await db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_class == "internal_system",
                Agent.name == HR_AGENT_NAME,
            )
        )
        existing = retry_result.scalar_one_or_none()
        if existing:
            return {"id": str(existing.id), "name": existing.name, "status": existing.status}
        raise HTTPException(status_code=500, detail="Failed to create HR agent")

    logger.info(f"[HR] Created HR agent {hr_agent.id} for tenant {tenant_id}")
    return {"id": str(hr_agent.id), "name": hr_agent.name, "status": hr_agent.status}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new digital employee (any authenticated user)."""
    # Determine target tenant: normally user's tenant; admins can override via payload
    target_tenant_id = current_user.tenant_id
    if current_user.role in ("platform_admin", "org_admin"):
        target_tenant_id = resolve_tenant_scope(current_user, data.tenant_id)

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

    agent = Agent(
        name=data.name,
        role_description=data.role_description,
        bio=data.bio,
        avatar_url=data.avatar_url,
        creator_id=current_user.id,
        tenant_id=target_tenant_id,
        agent_type="native",
        primary_model_id=data.primary_model_id,
        fallback_model_id=data.fallback_model_id,
        agent_class=data.agent_class,
        security_zone=data.security_zone,
        execution_mode=data.execution_mode,
        status="draft",
        max_triggers=default_max_triggers,
        min_poll_interval_min=default_min_poll,
        webhook_rate_limit=default_webhook_rate,
    )
    db.add(agent)
    await db.flush()

    # Lifecycle: draft → creating (validated by state machine)
    try:
        ctx = TransitionContext(is_system=True)
        agent.status = transition("draft", "creating", ctx)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Auto-create Participant identity for the new agent
    from app.models.participant import Participant
    db.add(Participant(
        type="agent", ref_id=agent.id,
        display_name=agent.name, avatar_url=agent.avatar_url,
    ))
    await db.flush()

    # Set permissions
    access_level = data.permission_access_level if data.permission_access_level in ("use", "manage") else "use"
    if data.permission_scope_type == "company":
        db.add(AgentPermission(agent_id=agent.id, scope_type="company", access_level=access_level))
    elif data.permission_scope_type == "user":
        if data.permission_scope_ids:
            for scope_id in data.permission_scope_ids:
                db.add(AgentPermission(agent_id=agent.id, scope_type="user", scope_id=scope_id, access_level=access_level))
        else:
            # "仅自己" — insert creator as the only permitted user
            db.add(AgentPermission(agent_id=agent.id, scope_type="user", scope_id=current_user.id, access_level="manage"))

    await db.flush()

    # Assign default platform tools
    from app.services.tool_seeder import assign_default_tools_to_agent
    await assign_default_tools_to_agent(db, agent.id)
    await db.flush()

    # Initialize agent — wrapped in try/except for rollback on failure (C-01)
    try:
        from app.services.agent_manager import agent_manager
        await agent_manager.initialize_agent_files(
            db, agent,
            personality=data.personality,
            boundaries=data.boundaries,
        )

        # Copy selected skills + mandatory default skills into agent workspace
        from app.models.skill import Skill
        from sqlalchemy.orm import selectinload

        default_result = await db.execute(select(Skill).where(Skill.is_default))
        default_ids = {s.id for s in default_result.scalars().all()}
        all_skill_ids = set(data.skill_ids or []) | default_ids

        if all_skill_ids:
            agent_dir = agent_manager._agent_dir(agent.id)
            skills_dir = agent_dir / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)

            for sid in all_skill_ids:
                result = await db.execute(
                    select(Skill).where(Skill.id == sid).options(selectinload(Skill.files))
                )
                skill = result.scalar_one_or_none()
                if not skill:
                    continue
                skill_folder = skills_dir / skill.folder_name
                skill_folder.mkdir(parents=True, exist_ok=True)
                for sf in skill.files:
                    file_path = skill_folder / sf.path
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(sf.content)

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
        await write_audit_event(db, event_type="agent.created", severity="info",
            actor_type="user", actor_id=current_user.id, tenant_id=current_user.tenant_id,
            action="create_agent", resource_type="agent", resource_id=agent.id,
            details={"name": agent.name, "agent_type": agent.agent_type})
    except Exception as _audit_err:
        logger.warning("Audit write failed for agent.created: %s", _audit_err)

    return AgentOut.model_validate(agent)

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
    out = AgentOut.model_validate(agent).model_dump()
    out["access_level"] = access_level

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
    agent, _access = await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    perms = result.scalars().all()

    if not perms:
        return {"scope_type": "user", "scope_ids": [], "access_level": "manage" if is_agent_creator(current_user, agent) else "use", "is_owner": is_agent_creator(current_user, agent)}

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
        "is_owner": is_agent_creator(current_user, agent),
    }


@router.put("/{agent_id}/permissions")
async def update_agent_permissions(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent permission scope (owner or platform_admin only)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owner or admin can change permissions")

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
        db.add(AgentPermission(agent_id=agent_id, scope_type="company", access_level=access_level))
    elif scope_type == "user":
        if scope_ids:
            for sid in scope_ids:
                db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=uuid.UUID(sid), access_level=access_level))
        else:
            # "仅自己"
            db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=current_user.id, access_level="manage"))

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

    is_admin = current_user.role in ("platform_admin", "org_admin")

    if not is_agent_creator(current_user, agent) and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can update agent settings")

    update_data = data.model_dump(exclude_unset=True)

    # Enforce heartbeat floor from tenant
    clamped_fields = []  # track fields adjusted by tenant floor
    if "heartbeat_interval_minutes" in update_data and current_user.tenant_id:
        from app.models.tenant import Tenant
        t_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant and update_data["heartbeat_interval_minutes"] < tenant.min_heartbeat_interval_minutes:
            update_data["heartbeat_interval_minutes"] = tenant.min_heartbeat_interval_minutes
            clamped_fields.append({
                "field": "heartbeat_interval_minutes",
                "requested": update_data["heartbeat_interval_minutes"],
                "applied": tenant.min_heartbeat_interval_minutes,
                "reason": "company_floor",
            })

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
                    clamped_fields.append({
                        "field": "min_poll_interval_min",
                        "requested": original,
                        "applied": update_data["min_poll_interval_min"],
                        "reason": "company_floor",
                    })
            if "webhook_rate_limit" in update_data:
                original = update_data["webhook_rate_limit"]
                update_data["webhook_rate_limit"] = min(original, tenant.max_webhook_rate_ceiling)
                if update_data["webhook_rate_limit"] != original:
                    clamped_fields.append({
                        "field": "webhook_rate_limit",
                        "requested": original,
                        "applied": update_data["webhook_rate_limit"],
                        "reason": "company_ceiling",
                    })

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
        await write_audit_event(db, event_type="agent.updated", severity="info",
            actor_type="user", actor_id=current_user.id, tenant_id=current_user.tenant_id,
            action="update_agent", resource_type="agent", resource_id=agent.id,
            details={"changed_fields": list(update_data.keys())})
    except Exception:
        logger.warning("Audit write failed for agent.updated", exc_info=True)

    out = AgentOut.model_validate(agent).model_dump()
    if clamped_fields:
        out["_clamped_fields"] = clamped_fields
    return out


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a digital employee (creator only)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can delete agent")

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

    # Delete related records that reference this agent
    # Use savepoints so a failure in one table doesn't poison the whole transaction
    from sqlalchemy import text, table, column

    # Whitelist of tables to clean — validated to prevent SQL injection
    _CLEANUP_TABLES = frozenset({
        "agent_activity_logs", "audit_logs", "approval_requests",
        "chat_messages", "chat_sessions", "tasks",
        "agent_schedules", "agent_triggers", "channel_configs",
        "agent_permissions", "agent_tools", "agent_relationships",
        "gateway_messages",
    })

    for table_name in _CLEANUP_TABLES:
        try:
            async with db.begin_nested():
                t = table(table_name, column("agent_id"))
                await db.execute(t.delete().where(t.c.agent_id == agent_id))
        except Exception as e:
            logger.debug("Cleanup skip %s for agent %s: %s", table_name, agent_id, e)

    # Clean up secondary FK columns that also reference agents table
    secondary_fk_cleanups = [
        "DELETE FROM chat_sessions WHERE peer_agent_id = :aid",
        "DELETE FROM gateway_messages WHERE sender_agent_id = :aid",
    ]
    for sql in secondary_fk_cleanups:
        try:
            async with db.begin_nested():
                await db.execute(text(sql), {"aid": agent_id})
        except Exception:
            logger.debug("FK cleanup skipped for agent %s: %s", agent_id, sql[:60])

    # Also clean agent_agent_relationships (has both agent_id and target_agent_id)
    try:
        async with db.begin_nested():
            await db.execute(
                text("DELETE FROM agent_agent_relationships WHERE agent_id = :aid OR target_agent_id = :aid"),
                {"aid": agent_id},
            )
    except Exception as e:
        logger.debug("Cleanup skip agent_agent_relationships for agent %s: %s", agent_id, e)

    # Also clear plaza posts by this agent
    try:
        async with db.begin_nested():
            await db.execute(text("DELETE FROM plaza_posts WHERE author_id = :aid"), {"aid": str(agent_id)})
    except Exception as e:
        logger.debug("Cleanup skip plaza_posts for agent %s: %s", agent_id, e)

    # Clean up Participant identity
    try:
        async with db.begin_nested():
            await db.execute(
                text("DELETE FROM participants WHERE type = 'agent' AND ref_id = :aid"),
                {"aid": agent_id},
            )
    except Exception as e:
        logger.debug("Cleanup skip participants for agent %s: %s", agent_id, e)

    # Audit: agent deleted (before commit so we still have agent data)
    try:
        from app.core.policy import write_audit_event
        await write_audit_event(db, event_type="agent.deleted", severity="warn",
            actor_type="user", actor_id=current_user.id, tenant_id=current_user.tenant_id,
            action="delete_agent", resource_type="agent", resource_id=agent.id,
            details={"name": agent.name})
    except Exception:
        logger.warning("Audit write failed for agent.deleted", exc_info=True)

    await db.delete(agent)
    await db.commit()


@router.post("/{agent_id}/start", response_model=AgentOut)
async def start_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start an agent's container."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator can start agent")

    from app.services.agent_manager import agent_manager
    await agent_manager.start_container(db, agent)
    await db.flush()

    try:
        from app.core.policy import write_audit_event
        await write_audit_event(db, event_type="agent.started", severity="info",
            actor_type="user", actor_id=current_user.id, tenant_id=current_user.tenant_id,
            action="start_agent", resource_type="agent", resource_id=agent.id,
            details={"name": agent.name})
    except Exception:
        logger.warning("Audit write failed for agent.started", exc_info=True)

    return AgentOut.model_validate(agent)


@router.post("/{agent_id}/stop", response_model=AgentOut)
async def stop_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop an agent's container."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator can stop agent")

    from app.services.agent_manager import agent_manager
    await agent_manager.stop_container(agent)
    await db.flush()

    try:
        from app.core.policy import write_audit_event
        await write_audit_event(db, event_type="agent.stopped", severity="info",
            actor_type="user", actor_id=current_user.id, tenant_id=current_user.tenant_id,
            action="stop_agent", resource_type="agent", resource_id=agent.id,
            details={"name": agent.name})
    except Exception:
        logger.warning("Audit write failed for agent.stopped", exc_info=True)

    return AgentOut.model_validate(agent)


# ─── Agent-Level Approvals ──────────────────────────────


@router.get("/{agent_id}/approvals")
async def list_agent_approvals(
    agent_id: uuid.UUID,
    status_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List approval requests for a specific agent. Only creator or admin can view."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only agent creator or admin can view approvals")

    from app.models.audit import ApprovalRequest
    query = select(ApprovalRequest).where(ApprovalRequest.agent_id == agent_id)
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


# ─── OpenClaw API Key Management ────────────────────────


@router.post("/{agent_id}/api-key")
async def generate_or_reset_api_key(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate or regenerate API key for an OpenClaw agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can manage API keys")
    if getattr(agent, "agent_type", "native") != "openclaw":
        raise HTTPException(status_code=400, detail="API keys are only available for OpenClaw agents")

    import hashlib
    import secrets

    raw_key = f"oc-{secrets.token_urlsafe(32)}"
    agent.api_key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    await db.commit()

    return {"api_key": raw_key, "message": "Save this key — it won't be shown again."}


@router.get("/{agent_id}/gateway-messages")
async def list_gateway_messages(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List recent gateway messages for an OpenClaw agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    from app.models.gateway_message import GatewayMessage
    result = await db.execute(
        select(GatewayMessage)
        .where(GatewayMessage.agent_id == agent_id)
        .order_by(GatewayMessage.created_at.desc())
        .limit(50)
    )
    messages = result.scalars().all()

    out = []
    for m in messages:
        sender_name = None
        if m.sender_agent_id:
            r = await db.execute(select(Agent.name).where(Agent.id == m.sender_agent_id))
            sender_name = r.scalar_one_or_none()
        out.append({
            "id": str(m.id),
            "sender_agent_name": sender_name,
            "content": m.content,
            "status": m.status,
            "result": m.result,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "delivered_at": m.delivered_at.isoformat() if m.delivered_at else None,
            "completed_at": m.completed_at.isoformat() if m.completed_at else None,
        })
    return out
