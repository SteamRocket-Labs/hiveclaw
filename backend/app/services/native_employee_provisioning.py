"""Shared creation of one full native employee.

Assistant opening calls this. Later natural creation should call it too,
instead of growing a second creation stack. Skills, MCP, plugins, API keys,
and budget rows are not installed here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent_lifecycle import InvalidTransitionError, TransitionContext, transition
from app.models.agent import Agent, AgentPermission
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_identity_lifecycle import ensure_agent_identity
from app.services.heartbeat_policy import apply_managed_heartbeat_fields

logger = logging.getLogger(__name__)


class ModelNotApproved(Exception):
    """The caller named a model this company cannot use. Do not substitute another."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class NativeEmployeeResult:
    agent: Agent
    workspace_ready: bool
    error_code: str | None


async def provision_native_employee(
    db: AsyncSession,
    *,
    requester: User,
    tenant_id: uuid.UUID,
    name: str,
    role_description: str,
    primary_model_id: uuid.UUID,
    audit_action: str,
) -> NativeEmployeeResult:
    """Create one native employee owned by the requester and finish its workspace."""

    await _reject_unapproved_model(db, tenant_id, primary_model_id)
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    agent = Agent(
        name=name[:100],
        role_description=role_description,
        creator_id=requester.id,
        sponsor_user_id=requester.id,
        owner_user_id=requester.id,
        tenant_id=tenant_id,
        agent_type="native",
        agent_class="internal_tenant",
        primary_model_id=primary_model_id,
        fallback_model_id=None,
        status="draft",
        max_triggers=(getattr(tenant, "default_max_triggers", None) or 20) if tenant else 20,
        min_poll_interval_min=(getattr(tenant, "min_poll_interval_floor", None) or 5) if tenant else 5,
        webhook_rate_limit=(getattr(tenant, "max_webhook_rate_ceiling", None) or 5) if tenant else 5,
    )
    apply_managed_heartbeat_fields(agent)
    db.add(agent)
    await ensure_agent_identity(
        db,
        agent,
        rls_bypass_reason="native employee identity bootstrap",
        rls_bypass_actor_id=str(requester.id),
    )
    try:
        agent.status = transition("draft", "creating", TransitionContext(is_system=True))
    except InvalidTransitionError:
        logger.exception("native employee left draft without a legal creating transition")
        raise
    db.add(
        AgentPermission(
            agent_id=agent.id,
            tenant_id=tenant_id,
            scope_type="user",
            scope_id=requester.id,
            access_level="manage",
        )
    )
    await db.flush()
    from app.services.tool_seeder import assign_default_tools_to_agent

    await assign_default_tools_to_agent(db, agent.id)
    await db.flush()
    return await resume_native_employee(db, agent, actor_id=requester.id, audit_action=audit_action)


async def resume_native_employee(
    db: AsyncSession,
    agent: Agent,
    *,
    actor_id: uuid.UUID,
    audit_action: str,
) -> NativeEmployeeResult:
    """Finish workspace and projection for an employee that already exists."""

    from app.services.agent_manager import agent_manager

    try:
        await agent_manager.initialize_agent_files(db, agent, original_author_id=actor_id)
    except Exception as exc:
        logger.warning("native employee workspace incomplete: %s", type(exc).__name__)
        return NativeEmployeeResult(agent=agent, workspace_ready=False, error_code="workspace_failed")
    try:
        await agent_manager.start_container(db, agent)
    except Exception as exc:
        logger.warning("native employee runtime bookkeeping failed: %s", type(exc).__name__)
    if agent.status == "creating":
        agent.status = "idle"
    await db.flush()
    await _record_side_effects(db, agent, actor_id=actor_id, audit_action=audit_action)
    return NativeEmployeeResult(agent=agent, workspace_ready=True, error_code=None)


async def _reject_unapproved_model(db: AsyncSession, tenant_id: uuid.UUID, model_id: uuid.UUID) -> None:
    from app.api.agents import _validate_model_refs

    try:
        await _validate_model_refs(
            db,
            tenant_id,
            primary_model_id=model_id,
            fallback_model_id=None,
        )
    except HTTPException as exc:
        raise ModelNotApproved(str(exc.detail)) from exc


async def _record_side_effects(
    db: AsyncSession,
    agent: Agent,
    *,
    actor_id: uuid.UUID,
    audit_action: str,
) -> None:
    try:
        async with db.begin_nested():
            from app.core.policy import write_audit_event

            await write_audit_event(
                db,
                event_type="agent.created",
                severity="info",
                actor_type="user",
                actor_id=actor_id,
                tenant_id=agent.tenant_id,
                action=audit_action,
                resource_type="agent",
                resource_id=agent.id,
                details={
                    "name": agent.name,
                    "agent_type": agent.agent_type,
                    "source": "approved_default",
                    "blueprint_click": False,
                    "hr_conversation": False,
                },
            )
    except Exception as exc:
        logger.warning("native employee audit was not recorded: %s", type(exc).__name__)
    try:
        async with db.begin_nested():
            from app.services.ai_asset_adapters import project_agent
            from app.services.ai_assets import register_projection

            await register_projection(
                db,
                project_agent(agent),
                change_source="create",
                actor_user_id=actor_id,
                change_message="Native employee provisioned",
            )
    except Exception as exc:
        logger.warning("native employee projection was not recorded: %s", type(exc).__name__)
