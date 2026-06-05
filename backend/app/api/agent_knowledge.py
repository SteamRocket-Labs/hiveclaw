"""Agent Knowledge read model API (spec §11 / §12 P7).

Six read-only endpoints over the memory engine. The frontend Knowledge plane
consumes these instead of parsing raw file layout; raw Markdown remains
available through the existing workspace file APIs as the advanced view.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/knowledge", tags=["agent-knowledge"])


def _data_root() -> Path:
    return Path(get_settings().AGENT_DATA_DIR)


def _principal_stack_for_read(agent: Agent, current_user: User) -> PrincipalStack:
    owner_id = getattr(agent, "owner_user_id", None) or getattr(agent, "creator_id", None)
    current_role = PrincipalRole.COMPANY_ADMIN if current_user.role == "org_admin" else PrincipalRole.CURRENT_USER
    current = Principal(
        role=current_role,
        id=str(current_user.id),
        label=getattr(current_user, "display_name", None) or getattr(current_user, "email", "") or "",
    )
    owner = Principal(role=PrincipalRole.OWNER, id=str(owner_id), label="") if owner_id else None
    creator_id = getattr(agent, "creator_id", None)
    creator = (
        Principal(role=PrincipalRole.CREATOR, id=str(creator_id), label="")
        if creator_id and creator_id != owner_id
        else None
    )
    company = (
        Principal(role=PrincipalRole.COMPANY, id=str(agent.tenant_id), label="")
        if getattr(agent, "tenant_id", None)
        else None
    )
    platform = (
        Principal(role=PrincipalRole.PLATFORM, id=str(current_user.id), label=current.label)
        if current_user.role == "platform_admin"
        else None
    )
    return PrincipalStack(
        platform=platform,
        company=company,
        direct_owner=owner,
        creator=creator,
        current_user=current,
    )


@router.get("/overview")
async def get_overview(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import build_knowledge_overview

    return build_knowledge_overview(_data_root(), agent_id)


@router.get("/pages")
async def get_pages(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_pages

    return {
        "pages": list_knowledge_pages(
            _data_root(), agent_id, principal_stack=_principal_stack_for_read(agent, current_user)
        )
    }


@router.get("/pages/{page_id:path}")
async def get_page(
    agent_id: uuid.UUID,
    page_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import get_knowledge_page

    page = get_knowledge_page(
        _data_root(), agent_id, page_id, principal_stack=_principal_stack_for_read(agent, current_user)
    )
    if page is None:
        raise HTTPException(status_code=404, detail="Knowledge page not found")
    return page


@router.get("/entries")
async def get_entries(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_entries

    return {
        "entries": list_knowledge_entries(
            _data_root(), agent_id, principal_stack=_principal_stack_for_read(agent, current_user)
        )
    }


@router.get("/events")
async def get_events(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_events

    return {"events": list_knowledge_events(_data_root(), agent_id)}


@router.get("/candidates")
async def get_candidates(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_candidates

    return list_knowledge_candidates(_data_root(), agent_id)
