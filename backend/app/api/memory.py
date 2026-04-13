"""Memory configuration API — manage tenant memory/summarization settings."""

import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.core.tenant_scope import resolve_tenant_scope
from app.database import get_db
from app.memory.store import PersistentMemoryStore
from app.models.chat_session import ChatSession
from app.models.tenant_setting import TenantSetting
from app.models.user import User

router = APIRouter(prefix="/enterprise/memory", tags=["memory"])


class MemoryConfigUpdate(BaseModel):
    summary_model_id: str | None = None
    rerank_model_id: str | None = None
    compress_threshold: int = 70  # percentage
    keep_recent: int = 10
    extract_to_viking: bool = False


class TeamMemoryUpsert(BaseModel):
    workspace_key: str
    key: str
    title: str
    content: str
    mode: Literal["replace", "append"] = "replace"
    base_revision: int | None = None
    sync_token: str | None = None


def _team_memory_conflict_detail(exc) -> dict:
    current_entry = asdict(exc.current_entry)
    return {
        "message": str(exc),
        "retry_action": "pull_latest_then_retry",
        "sync_hint": "server_wins_pull",
        "server_sync_token": current_entry.get("sync_token"),
        "current_entry": current_entry,
    }


@router.get("/config")
async def get_memory_config(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tenant memory configuration."""
    target_tenant_id = resolve_tenant_scope(current_user, tenant_id)
    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == target_tenant_id,
            TenantSetting.key == "memory_config",
        )
    )
    setting = result.scalar_one_or_none()
    if not setting:
        return {
            "summary_model_id": None,
            "rerank_model_id": None,
            "compress_threshold": 70,
            "keep_recent": 10,
            "extract_to_viking": False,
        }
    return setting.value


@router.put("/config")
async def update_memory_config(
    data: MemoryConfigUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update tenant memory configuration (admin only)."""
    target_tenant_id = resolve_tenant_scope(current_user, tenant_id)

    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == target_tenant_id,
            TenantSetting.key == "memory_config",
        )
    )
    setting = result.scalar_one_or_none()

    config_dict = data.model_dump()

    # Validate model IDs belong to the same tenant
    from app.models.llm import LLMModel
    for field_name, model_id in [("summary_model_id", data.summary_model_id), ("rerank_model_id", data.rerank_model_id)]:
        if model_id:
            model_r = await db.execute(
                select(LLMModel.id).where(
                    LLMModel.id == model_id,
                    LLMModel.tenant_id == target_tenant_id,
                )
            )
            if not model_r.scalar_one_or_none():
                raise HTTPException(status_code=400, detail=f"{field_name} model not found in your tenant")

    if setting:
        setting.value = config_dict
    else:
        setting = TenantSetting(
            tenant_id=target_tenant_id,
            key="memory_config",
            value=config_dict,
        )
        db.add(setting)

    await db.commit()
    return config_dict


@router.get("/agents/{agent_id}/memory")
async def get_agent_memory(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """View agent's structured memory facts (requires agent access)."""
    from app.core.permissions import check_agent_access
    from app.config import get_settings

    await check_agent_access(db, current_user, agent_id)

    settings = get_settings()
    store = PersistentMemoryStore(data_root=Path(settings.AGENT_DATA_DIR))
    return {"facts": store.load_semantic_facts(agent_id)}


@router.get("/sessions/{session_id}/summary")
async def get_session_summary(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """View session summary (owner-only access)."""
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": str(session.id),
        "summary": session.summary,
        "title": session.title,
    }


@router.get("/shared")
async def list_team_memory(
    workspace_key: str,
    include_deleted: bool = False,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
):
    """List shared team-memory entries for the current tenant/workspace."""
    from app.services.team_memory import TeamMemoryStore

    target_tenant_id = resolve_tenant_scope(current_user, tenant_id)
    store = TeamMemoryStore()
    return [asdict(entry) for entry in store.list_entries(str(target_tenant_id), workspace_key, include_deleted=include_deleted)]


@router.get("/shared/search")
async def search_team_memory(
    workspace_key: str,
    query: str,
    include_deleted: bool = False,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
):
    """Search shared team-memory entries for the current tenant/workspace."""
    from app.services.team_memory import TeamMemoryStore

    target_tenant_id = resolve_tenant_scope(current_user, tenant_id)
    store = TeamMemoryStore()
    return [
        asdict(entry)
        for entry in store.search_entries(str(target_tenant_id), workspace_key, query, include_deleted=include_deleted)
    ]


@router.get("/shared/{entry_key}")
async def get_team_memory_entry(
    entry_key: str,
    workspace_key: str,
    include_deleted: bool = False,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
):
    """Fetch a single shared team-memory entry with full content."""
    from app.services.team_memory import TeamMemoryStore

    target_tenant_id = resolve_tenant_scope(current_user, tenant_id)
    store = TeamMemoryStore()
    entry = store.get_entry(str(target_tenant_id), workspace_key, entry_key, include_deleted=include_deleted)
    if entry is None:
        raise HTTPException(status_code=404, detail="Shared memory entry not found")
    return asdict(entry)


@router.put("/shared")
async def upsert_team_memory(
    data: TeamMemoryUpsert,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
):
    """Create or update a shared team-memory entry for the current tenant/workspace."""
    from app.services.team_memory import (
        InvalidTeamMemoryModeError,
        SecretScanError,
        TeamMemoryConflictError,
        TeamMemoryStore,
    )

    target_tenant_id = resolve_tenant_scope(current_user, tenant_id)
    store = TeamMemoryStore()
    try:
        entry = store.upsert_entry(
            tenant_id=str(target_tenant_id),
            workspace_key=data.workspace_key,
            key=data.key,
            title=data.title,
            content=data.content,
            mode=data.mode,
            updated_by=str(current_user.id),
            base_revision=data.base_revision,
            sync_token=data.sync_token,
        )
    except SecretScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InvalidTeamMemoryModeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TeamMemoryConflictError as exc:
        raise HTTPException(status_code=409, detail=_team_memory_conflict_detail(exc)) from exc
    return asdict(entry)


@router.delete("/shared/{entry_key}")
async def delete_team_memory(
    entry_key: str,
    workspace_key: str,
    base_revision: int | None = None,
    sync_token: str | None = None,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
):
    """Delete a shared team-memory entry for the current tenant/workspace."""
    from app.services.team_memory import TeamMemoryConflictError, TeamMemoryStore

    target_tenant_id = resolve_tenant_scope(current_user, tenant_id)
    store = TeamMemoryStore()
    try:
        deleted = store.delete_entry(
            str(target_tenant_id),
            workspace_key,
            entry_key,
            updated_by=str(current_user.id),
            base_revision=base_revision,
            sync_token=sync_token,
        )
    except TeamMemoryConflictError as exc:
        raise HTTPException(status_code=409, detail=_team_memory_conflict_detail(exc)) from exc
    return {"deleted": deleted, "key": entry_key, "workspace_key": workspace_key}
