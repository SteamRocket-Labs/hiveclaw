"""Canonical Agent ownership mutation service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import effective_agent_owner_id, require_agent_owner_or_admin
from app.models.agent import Agent
from app.models.agent_collaboration import AgentCollaborationGroupMember
from app.models.audit import AuditLog
from app.models.user import User
from app.services.ai_assets import register_agent_asset


@dataclass(frozen=True, slots=True)
class AgentOwnerTransferReceipt:
    status: str
    agent_id: uuid.UUID
    agent_name: str
    old_owner_id: uuid.UUID | None
    new_owner_id: uuid.UUID
    new_owner_name: str
    mode: str
    request_id: str | None = None


def _same_uuid(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


async def _rebind_active_collaboration_memberships(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    new_owner_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Move live A2A owner gates to the new owner and require reconfirmation."""

    members = list(
        (
            await db.execute(
                select(AgentCollaborationGroupMember)
                .where(
                    AgentCollaborationGroupMember.agent_id == agent_id,
                    AgentCollaborationGroupMember.status.in_(("active", "pending_owner_confirmation")),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for member in members:
        member.agent_owner_user_id = new_owner_id
        member.status = "pending_owner_confirmation"
        member.approved_by_user_id = None
        member.approved_at = None
        member.rejected_at = None
        member.revoked_at = None
    return [member.id for member in members]


async def transfer_loaded_agent_owner(
    db: AsyncSession,
    *,
    agent: Agent,
    new_owner: User,
    actor: User,
    reason: str,
    expected_owner_id: uuid.UUID | None,
    mode: str,
    request_id: str | None = None,
    flush: bool = True,
) -> AgentOwnerTransferReceipt:
    """Change only current ownership on already-authorized, locked rows."""

    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise HTTPException(status_code=400, detail="Ownership transfer reason is required")
    if not getattr(new_owner, "is_active", False):
        raise HTTPException(status_code=400, detail="Target owner is inactive")
    if not _same_uuid(getattr(new_owner, "tenant_id", None), getattr(agent, "tenant_id", None)):
        raise HTTPException(status_code=400, detail="Target owner must belong to the same company")

    old_owner_id = effective_agent_owner_id(agent)
    if expected_owner_id is not None and not _same_uuid(expected_owner_id, old_owner_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "agent_owner_changed",
                "agent_id": str(agent.id),
                "expected_owner_id": str(expected_owner_id),
                "current_owner_id": str(old_owner_id) if old_owner_id else None,
            },
        )

    transfer_status = "unchanged" if _same_uuid(old_owner_id, new_owner.id) else "transferred"
    if transfer_status == "transferred":
        # Creator and sponsor are immutable provenance. Only current ownership
        # changes here and every mutation passes through this single service.
        agent.owner_user_id = new_owner.id
        rebound_membership_ids = await _rebind_active_collaboration_memberships(
            db,
            agent_id=agent.id,
            new_owner_id=new_owner.id,
        )
        db.add(
            AuditLog(
                user_id=actor.id,
                agent_id=agent.id,
                tenant_id=agent.tenant_id,
                action="agent:handover",
                details={
                    "schema": "hive.agent_owner_transfer.v1",
                    "creator_id": str(agent.creator_id),
                    "sponsor_user_id": str(agent.sponsor_user_id),
                    "from_owner": str(old_owner_id) if old_owner_id else None,
                    "to_owner": str(new_owner.id),
                    "reason": clean_reason,
                    "mode": str(mode),
                    "request_id": request_id,
                    "a2a_memberships_pending_reconfirmation": [
                        str(value) for value in rebound_membership_ids
                    ],
                },
            )
        )
        await register_agent_asset(
            db,
            agent,
            change_source="owner_transfer",
            actor_user_id=actor.id,
            change_message=f"Agent owner transferred via {mode}: {clean_reason}",
        )
        from app.services.workspace_sync_dirty import mark_agent_dirty

        mark_agent_dirty(agent.id)
        if flush:
            await db.flush()

    return AgentOwnerTransferReceipt(
        status=transfer_status,
        agent_id=agent.id,
        agent_name=agent.name,
        old_owner_id=old_owner_id,
        new_owner_id=new_owner.id,
        new_owner_name=new_owner.display_name,
        mode=str(mode),
        request_id=request_id,
    )


async def transfer_agent_owner(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    new_owner_id: uuid.UUID,
    actor: User,
    reason: str,
    expected_owner_id: uuid.UUID | None = None,
    mode: str = "manual_admin",
    request_id: str | None = None,
) -> AgentOwnerTransferReceipt:
    """Authorize, lock, validate, mutate, and audit one ownership transfer."""

    agent = await require_agent_owner_or_admin(db, actor, agent_id, lock=True)
    new_owner = (
        await db.execute(select(User).where(User.id == new_owner_id).with_for_update())
    ).scalar_one_or_none()
    if new_owner is None:
        raise HTTPException(status_code=404, detail="Target owner not found")
    return await transfer_loaded_agent_owner(
        db,
        agent=agent,
        new_owner=new_owner,
        actor=actor,
        reason=reason,
        expected_owner_id=expected_owner_id,
        mode=mode,
        request_id=request_id,
    )
