"""Agent-scoped hook inspection and runtime config API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.runtime.hooks import configure_hook_runtime, describe_hook_runtime_config, hook_registry

router = APIRouter(prefix="/agents/{agent_id}/hooks", tags=["hooks"])


class HookRuntimeConfigIn(BaseModel):
    enabled: bool | None = None
    timeout_seconds: float | None = Field(default=None, ge=0)
    failure_policy: str | None = None


@router.get("")
async def list_agent_hooks(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    registrations = hook_registry.describe_registrations()
    config_by_key = {item["key"]: item for item in describe_hook_runtime_config().get("items", []) if item.get("key")}
    return {
        "events": sorted({item["event"] for item in registrations}),
        "registrations": [
            {
                **item,
                "runtime_config": config_by_key.get(
                    item.get("key"),
                    {
                        "key": item.get("key"),
                        "enabled": True,
                        "timeout_seconds": None,
                        "failure_policy": "continue",
                    },
                ),
            }
            for item in registrations
        ],
    }


@router.patch("/{hook_key}")
async def update_agent_hook_runtime_config(
    agent_id: uuid.UUID,
    hook_key: str,
    body: HookRuntimeConfigIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level not in {"manage", "owner"} and getattr(current_user, "role", None) not in {
        "platform_admin",
        "org_admin",
    }:
        raise HTTPException(status_code=403, detail="Hook config requires manage access")
    try:
        config = configure_hook_runtime(
            key=hook_key,
            enabled=body.enabled,
            timeout_seconds=body.timeout_seconds,
            failure_policy=body.failure_policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "config": config}
