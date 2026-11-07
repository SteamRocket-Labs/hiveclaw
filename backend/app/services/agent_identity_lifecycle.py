"""Agent identity and lifecycle invariants.

This module owns the durable link between an Agent row, its current owner,
immutable creation/delegation provenance, and its Participant identity.
Creation paths should call ``ensure_agent_identity``
before the first flush. Digital employees are never deleted (G1): retirement
is a deactivation, and ``terminate_agent_runtime_tasks`` settles the work a
deactivated employee still has running.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, inspect as sqlalchemy_inspect, or_, select
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import enter_rls_bypass
from app.models.agent import Agent
from app.models.participant import Participant
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User

_ACTIVE_RUNTIME_TASK_STATUSES = ("pending", "running")


def _uuid_or_new(value: uuid.UUID | None) -> uuid.UUID:
    return value if value is not None else uuid.uuid4()


def _loaded_relationship(instance: Any, name: str) -> Any | None:
    """Read an ORM relationship only when already loaded.

    Lifecycle checks are also used on detached runtime payloads. Triggering a
    lazy load here would either perform hidden IO or raise MissingGreenlet /
    DetachedInstanceError, so production entry points eager-load the owner and
    this helper treats an unavailable projection as unknown rather than active
    evidence of deactivation.
    """

    try:
        state = sqlalchemy_inspect(instance)
    except NoInspectionAvailable:
        return getattr(instance, name, None)
    if name in state.unloaded:
        return None
    return getattr(instance, name, None)


def get_agent_lifecycle_block_reason(agent: Any) -> str | None:
    """Return why an agent may not be used, or ``None`` when active."""
    if getattr(agent, "deleted_at", None) is not None:
        return "deleted"
    if getattr(agent, "deactivated_at", None) is not None:
        return "deactivated"

    # ``sponsor_user_id`` is immutable delegation provenance. Runtime
    # availability follows the mutable current owner, with creator used only
    # for legacy rows that predate ``owner_user_id``.
    owner_user_id = getattr(agent, "owner_user_id", None)
    owner = _loaded_relationship(agent, "owner" if owner_user_id is not None else "creator")
    if owner is not None and getattr(owner, "is_active", True) is False:
        return "inactive_owner"

    owner_is_active = getattr(agent, "owner_is_active", None)
    if owner_is_active is False:
        return "inactive_owner"

    return None


def is_agent_lifecycle_active(agent: Any) -> bool:
    return get_agent_lifecycle_block_reason(agent) is None


@dataclass(frozen=True, slots=True)
class LiveEffectPrincipal:
    """The user who may authorize a new effect for an agent right now."""

    user_id: uuid.UUID | None
    block_reason: str | None


async def resolve_live_effect_principal(db: AsyncSession, agent: Any) -> LiveEffectPrincipal:
    """Resolve the current responsible user without rewriting historical identity.

    ``creator_id`` and ``sponsor_user_id`` stay provenance. A missing, inactive,
    or cross-company responsible user blocks the new effect.
    """

    from app.core.permissions import effective_agent_owner_id

    owner_id = effective_agent_owner_id(agent)
    if owner_id is None:
        return LiveEffectPrincipal(user_id=None, block_reason="responsible_principal_missing")
    user = (await db.execute(select(User).where(User.id == owner_id)
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if user is None:
        return LiveEffectPrincipal(user_id=None, block_reason="responsible_principal_missing")
    if getattr(user, "is_active", True) is False:
        return LiveEffectPrincipal(user_id=owner_id, block_reason="responsible_principal_inactive")
    agent_tenant_id = getattr(agent, "tenant_id", None)
    user_tenant_id = getattr(user, "tenant_id", None)
    if agent_tenant_id is None or user_tenant_id is None or str(agent_tenant_id) != str(user_tenant_id):
        return LiveEffectPrincipal(user_id=owner_id, block_reason="responsible_principal_tenant_mismatch")
    return LiveEffectPrincipal(user_id=owner_id, block_reason=None)


def agent_lifecycle_active_clause(*, include_deactivated: bool = False):
    """SQLAlchemy filter for rows that may be shown or autonomously executed."""
    return and_(
        Agent.deleted_at.is_(None),
        True if include_deactivated else Agent.deactivated_at.is_(None),
        # An Agent whose company is retired/missing is not shown or executed,
        # even when its own lifecycle fields look active.
        Agent.tenant_id.in_(select(Tenant.id).where(Tenant.is_active.is_(True))),
        or_(
            Agent.owner_user_id.in_(select(User.id).where(User.is_active.is_(True))),
            and_(
                Agent.owner_user_id.is_(None),
                Agent.creator_id.in_(select(User.id).where(User.is_active.is_(True))),
            ),
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

    # Legacy rows were historically repaired from owner first. This is only a
    # provenance backfill and never participates in current Owner authority.
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


async def terminate_agent_runtime_tasks(
    db: AsyncSession,
    agent: Agent,
    *,
    actor_id: uuid.UUID,
    reason: str,
    terminal_source: str,
) -> None:
    """Settle a deactivated employee's unfinished native RuntimeTasks.

    G1 deactivation's settlement: native work still running when the employee
    is deactivated is withdrawn; nothing is deleted, no file moves, and a
    remote A2A task (excluded by the schema filter below) never has its
    unknown outcome converted into a native stop receipt — that work is the
    recall flow's to settle.

    Canonical advisory-first batch order: the shared terminal settlement
    acquires each task's session advisory, so THIS caller must already hold
    every affected session advisory BEFORE taking the RuntimeTask row locks
    below — locking the rows first would be the row → advisory side of the
    global order and deadlocks against any concurrent advisory-first writer
    of the same session (in-flight transcript append, Stop fence, canonical
    web-chat terminal writer). Sorted multi-session acquisition keeps two
    batch writers from cycling among themselves.

    Canonical advisory → row batch order with late-admission closure: the
    helper holds every affected session advisory before the first row lock
    and, when a task is admitted between its prescan and the FOR UPDATE
    batch, rolls back to a savepoint and rescans so the newcomer's session
    advisory is acquired BEFORE its row. The previous trailing
    ``late_session_ids`` acquisition took advisories while rows were
    already held — a real row → advisory edge (demonstrated deadlock by the
    CC5 late-admission probe) — and is gone.
    """
    from app.services.agent_retirement import guard_remote_admission
    from app.services.runtime_terminal_settlement import (
        lock_runtime_task_batch_with_session_authority,
        settle_and_enqueue_runtime_task_terminal,
    )

    await guard_remote_admission(db, agent_ids=[agent.id], retiring=True)
    # Remote A2A work is the recall flow's to settle (``prepare`` withdrew it
    # or refused); this settlement never converts a remote task's unknown
    # outcome into a native stop receipt.
    from app.services.remote_a2a_tasks import SCHEMA as REMOTE_A2A_SCHEMA

    runtime_tasks = await lock_runtime_task_batch_with_session_authority(
        db,
        statement=select(RuntimeTask).where(
            RuntimeTask.parent_agent_id == agent.id,
            RuntimeTask.status.in_(_ACTIVE_RUNTIME_TASK_STATUSES),
            # coalesce: a task without a schema key is native by definition,
            # and SQL NULL != 'x' would otherwise silently drop it.
            func.coalesce(RuntimeTask.metadata_json["schema"].as_string(), "") != REMOTE_A2A_SCHEMA,
        ),
    )
    now = datetime.now(timezone.utc)
    for task in runtime_tasks:
        task.status = "killed"
        task.completed_at = now
        task.result_summary = f"Agent stopped by {actor_id}: {reason}"
        task.claim_version = int(task.claim_version or 0) + 1
        task.claimed_by = None
        task.claim_expires_at = None
        await settle_and_enqueue_runtime_task_terminal(
            db,
            task,
            terminal_source=terminal_source,
            root_reason_code=reason,
        )
    await db.flush()


