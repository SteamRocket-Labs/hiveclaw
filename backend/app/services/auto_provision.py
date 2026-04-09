"""Auto-provision a user's main agent from role templates.

This keeps the old `ensure_main_agent(...)` contract alive while mapping it to
the current Agent/AgentTemplate model semantics:
- Main agent == owned by the user and has no parent_agent_id
- Department template wins over tenant default template
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentTemplate
from app.models.user import User
from app.services.sync_service import bump_sync_version


async def ensure_main_agent(db: AsyncSession, user: User) -> Agent | None:
    """Ensure the user has a provisioned main agent.

    Returns the existing/provisioned agent, or None when no suitable template
    exists or the user is not assigned to a tenant.
    """
    if not user.tenant_id:
        return None

    existing_result = await db.execute(
        select(Agent).where(
            Agent.owner_user_id == user.id,
            Agent.tenant_id == user.tenant_id,
            Agent.parent_agent_id.is_(None),
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return existing

    template = None
    if user.department_id:
        template_result = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.tenant_id == user.tenant_id,
                AgentTemplate.department_id == user.department_id,
            )
        )
        template = template_result.scalar_one_or_none()

    if template is None:
        template_result = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.tenant_id == user.tenant_id,
                AgentTemplate.department_id.is_(None),
            )
        )
        template = template_result.scalar_one_or_none()

    if template is None:
        return None

    agent = Agent(
        name=template.name,
        role_description=template.description or "",
        creator_id=user.id,
        tenant_id=user.tenant_id,
        primary_model_id=template.model_id,
        template_id=template.id,
        owner_user_id=user.id,
        channel_perms=True,
        config_version=1,
    )
    db.add(agent)
    await db.flush()

    await bump_sync_version(db, user.tenant_id)
    return agent
