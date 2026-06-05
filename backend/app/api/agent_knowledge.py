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
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/knowledge", tags=["agent-knowledge"])


def _data_root() -> Path:
    return Path(get_settings().AGENT_DATA_DIR)


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
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_pages

    return {"pages": list_knowledge_pages(_data_root(), agent_id)}


@router.get("/pages/{page_id:path}")
async def get_page(
    agent_id: uuid.UUID,
    page_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import get_knowledge_page

    page = get_knowledge_page(_data_root(), agent_id, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Knowledge page not found")
    return page


@router.get("/entries")
async def get_entries(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_entries

    return {"entries": list_knowledge_entries(_data_root(), agent_id)}


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
