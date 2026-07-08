"""Agent identity and lifecycle invariants.

This module owns the durable link between an Agent row, its sponsor user, and
its Participant identity. Creation paths should call ``ensure_agent_identity``
before the first flush; delete paths should call ``soft_delete_agent`` instead
of deleting agent/participant rows.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import enter_rls_bypass
from app.models.agent import Agent
from app.models.participant import Participant
from app.models.runtime_task import RuntimeTask
from app.models.schedule import AgentSchedule
from app.models.trigger import AgentTrigger
from app.models.user import User

_ACTIVE_RUNTIME_TASK_STATUSES = ("pending", "running")


def _uuid_or_new(value: uuid.UUID | None) -> uuid.UUID:
    return value if value is not None else uuid.uuid4()


def get_agent_lifecycle_block_reason(agent: Any) -> str | None:
    """Return why an agent may not be used, or ``None`` when active."""
    if getattr(agent, "deleted_at", None) is not None:
        return "deleted"
    if getattr(agent, "deactivated_at", None) is not None:
        return "deactivated"

    sponsor = getattr(agent, "sponsor", None)
    if sponsor is not None and getattr(sponsor, "is_active", True) is False:
        return "inactive_sponsor"

    sponsor_is_active = getattr(agent, "sponsor_is_active", None)
    if sponsor_is_active is False:
        return "inactive_sponsor"

    return None


def is_agent_lifecycle_active(agent: Any) -> bool:
    return get_agent_lifecycle_block_reason(agent) is None


def agent_lifecycle_active_clause():
    """SQLAlchemy filter for rows that may be shown or autonomously executed."""
    return and_(
        Agent.deleted_at.is_(None),
        Agent.deactivated_at.is_(None),
        or_(
            Agent.sponsor_user_id.is_(None),
            Agent.sponsor_user_id.in_(select(User.id).where(User.is_active.is_(True))),
        ),
    )


async def ensure_agent_identity(
    db: AsyncSession,
    agent: Agent,
    *,
    display_name: str | None = None,
    avatar_url: str | None = None,
    rls_bypass_reason: str | None = None,
    rls_bypass_actor_id: str | None = None,
) -> uuid.UUID:
    """Ensure an agent has a sponsor and a stable Participant identity.

    Safe for both pre-flush creation and legacy backfill. The helper assigns a
    UUID to ``agent.id`` before flush when needed so ``participants.ref_id`` can
    be written atomically with the Agent row.

    ``participants`` is a derived global identity table: its RLS policy checks
    the referenced user/agent row. Creating a brand-new Agent and its Participant
    is therefore circular under strict RLS because ``agents.participant_id``
    points to the participant while the participant policy references the agent.
    Creation paths may pass ``rls_bypass_reason`` to make that bootstrap an
    explicit audited boundary instead of an implicit policy failure.
    """
    if rls_bypass_reason:
        async with enter_rls_bypass(db, reason=rls_bypass_reason, actor_id=rls_bypass_actor_id) as bypass_db:
            return await ensure_agent_identity(
                bypass_db,
                agent,
                display_name=display_name,
                avatar_url=avatar_url,
            )

    if getattr(agent, "id", None) is None:
        agent.id = uuid.uuid4()

    sponsor_id = getattr(agent, "sponsor_user_id", None) or getattr(agent, "owner_user_id", None) or agent.creator_id
    agent.sponsor_user_id = sponsor_id

    participant = None
    no_autoflush = getattr(db, "no_autoflush", contextlib.nullcontext())
    with no_autoflush:
        if getattr(agent, "participant_id", None) is not None:
            result = await db.execute(select(Participant).where(Participant.id == agent.participant_id))
            participant = result.scalar_one_or_none()

        if participant is None:
            result = await db.execute(
                select(Participant).where(
                    Participant.type == "agent",
                    Participant.ref_id == agent.id,
                )
            )
            participant = result.scalar_one_or_none()

    if participant is None:
        participant = Participant(
            id=uuid.uuid4(),
            type="agent",
            ref_id=agent.id,
            display_name=(display_name or agent.name or "Agent")[:100],
            avatar_url=avatar_url if avatar_url is not None else getattr(agent, "avatar_url", None),
        )
        db.add(participant)
    else:
        participant.display_name = (display_name or agent.name or participant.display_name or "Agent")[:100]
        participant.avatar_url = avatar_url if avatar_url is not None else getattr(agent, "avatar_url", None)

    agent.participant_id = participant.id
    await db.flush()
    return participant.id


async def soft_delete_agent(
    db: AsyncSession,
    agent: Agent,
    *,
    actor_id: uuid.UUID,
    reason: str = "agent_deleted",
) -> None:
    """Soft-delete an agent while preserving audit/chat/participant history."""
    now = datetime.now(timezone.utc)
    agent.deleted_at = agent.deleted_at or now
    agent.deactivated_at = agent.deactivated_at or agent.deleted_at
    agent.deactivation_reason = reason
    agent.status = "stopped"
    agent.container_id = None
    agent.container_port = None

    await db.execute(
        update(AgentTrigger)
        .where(AgentTrigger.agent_id == agent.id, AgentTrigger.is_enabled.is_(True))
        .values(is_enabled=False)
    )
    await db.execute(
        update(AgentSchedule)
        .where(AgentSchedule.agent_id == agent.id, AgentSchedule.is_enabled.is_(True))
        .values(is_enabled=False)
    )
    await db.execute(
        update(RuntimeTask)
        .where(
            RuntimeTask.parent_agent_id == agent.id,
            RuntimeTask.status.in_(_ACTIVE_RUNTIME_TASK_STATUSES),
        )
        .values(
            status="killed",
            completed_at=now,
            result_summary=f"Agent soft-deleted by {actor_id}: {reason}",
        )
    )
    await db.flush()
