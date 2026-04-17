"""Agent relationship management API — human + agent-to-agent."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import get_current_user
from app.core.permissions import check_agent_access
from app.database import get_db
from app.models.org import AgentRelationship, AgentAgentRelationship
from app.models.user import User
from app.services.relationships_file import (
    AGENT_RELATION_LABELS,
    RELATION_LABELS,
    write_relationships_file,
)
router = APIRouter(prefix="/agents/{agent_id}/relationships", tags=["relationships"])


# ─── Schemas ───────────────────────────────────────────

class RelationshipIn(BaseModel):
    member_id: str
    relation: str = "collaborator"
    description: str = ""


class RelationshipBatchIn(BaseModel):
    relationships: list[RelationshipIn]


class AgentRelationshipIn(BaseModel):
    target_agent_id: str
    relation: str = "collaborator"
    description: str = ""


class AgentRelationshipBatchIn(BaseModel):
    relationships: list[AgentRelationshipIn]


# ─── Human Relationships (existing) ───────────────────

@router.get("/")
async def get_relationships(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all human relationships for this agent."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentRelationship)
        .where(AgentRelationship.agent_id == agent_id)
        .options(selectinload(AgentRelationship.member))
    )
    rels = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "member_id": str(r.member_id),
            "relation": r.relation,
            "relation_label": RELATION_LABELS.get(r.relation, r.relation),
            "description": r.description,
            "member": {
                "name": r.member.name,
                "title": r.member.title,
                "department_path": r.member.department_path,
                "avatar_url": r.member.avatar_url,
                "email": r.member.email,
            } if r.member else None,
        }
        for r in rels
    ]


@router.put("/")
async def save_relationships(
    agent_id: uuid.UUID,
    data: RelationshipBatchIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace all human relationships for this agent."""
    await check_agent_access(db, current_user, agent_id)

    await db.execute(
        delete(AgentRelationship).where(AgentRelationship.agent_id == agent_id)
    )

    for r in data.relationships:
        db.add(AgentRelationship(
            agent_id=agent_id,
            member_id=uuid.UUID(r.member_id),
            relation=r.relation,
            description=r.description,
        ))

    await db.flush()

    # Regenerate file with both types
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok"}


@router.delete("/{rel_id}")
async def delete_relationship(
    agent_id: uuid.UUID,
    rel_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single human relationship."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentRelationship).where(AgentRelationship.id == rel_id, AgentRelationship.agent_id == agent_id)
    )
    rel = result.scalar_one_or_none()
    if rel:
        await db.delete(rel)
        await db.flush()
        await _regenerate_relationships_file(db, agent_id)
        await db.commit()

    return {"status": "ok"}


# ─── Agent-to-Agent Relationships (new) ───────────────

@router.get("/agents")
async def get_agent_relationships(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all agent-to-agent relationships."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent_id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    rels = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "target_agent_id": str(r.target_agent_id),
            "relation": r.relation,
            "relation_label": AGENT_RELATION_LABELS.get(r.relation, r.relation),
            "description": r.description,
            "target_agent": {
                "id": str(r.target_agent.id),
                "name": r.target_agent.name,
                "role_description": r.target_agent.role_description or "",
                "avatar_url": r.target_agent.avatar_url or "",
            } if r.target_agent else None,
        }
        for r in rels
    ]


@router.put("/agents")
async def save_agent_relationships(
    agent_id: uuid.UUID,
    data: AgentRelationshipBatchIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace all agent-to-agent relationships."""
    await check_agent_access(db, current_user, agent_id)

    await db.execute(
        delete(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == agent_id)
    )

    for r in data.relationships:
        target_id = uuid.UUID(r.target_agent_id)
        if target_id == agent_id:
            continue  # skip self-reference
        db.add(AgentAgentRelationship(
            agent_id=agent_id,
            target_agent_id=target_id,
            relation=r.relation,
            description=r.description,
        ))

    await db.flush()
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok"}


@router.delete("/agents/{rel_id}")
async def delete_agent_relationship(
    agent_id: uuid.UUID,
    rel_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single agent-to-agent relationship."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentAgentRelationship).where(
            AgentAgentRelationship.id == rel_id,
            AgentAgentRelationship.agent_id == agent_id,
        )
    )
    rel = result.scalar_one_or_none()
    if rel:
        await db.delete(rel)
        await db.flush()
        await _regenerate_relationships_file(db, agent_id)
        await db.commit()

    return {"status": "ok"}


# ─── relationships.md Generation ──────────────────────

async def _regenerate_relationships_file(db: AsyncSession, agent_id: uuid.UUID):
    """Regenerate relationships.md with the shared explicit-relationship writer.

    Inline write covers the editing user; mark_agent_dirty broadcasts to
    peer backend instances so their workspace caches converge.
    """
    from app.services.workspace_sync_dirty import mark_agent_dirty

    await write_relationships_file(db=db, agent_id=agent_id, include_owner=True)
    mark_agent_dirty(agent_id)
