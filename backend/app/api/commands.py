"""Unified command surface API."""

from __future__ import annotations

import uuid
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.command_registry import build_default_command_registry
from app.services.agent_tools import execute_tool

router = APIRouter(prefix="/agents/{agent_id}/commands", tags=["commands"])


class ExecuteCommandIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


_EXECUTABLE_BUILTIN_COMMANDS = frozenset(
    {
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "task_output",
        "task_stop",
        "goal_start",
        "team_create",
        "advanced_plan",
    }
)


@router.get("")
async def list_agent_commands(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await check_agent_access(db, current_user, agent_id)
    return build_default_command_registry().visible_index(surface="agent_prompt")


@router.get("/{command_name}")
async def get_agent_command(
    agent_id: uuid.UUID,
    command_name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    registry = build_default_command_registry()
    try:
        return registry.get(command_name).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Command not found") from exc


@router.post("/{command_name}/execute")
async def execute_agent_command(
    agent_id: uuid.UUID,
    command_name: str,
    body: ExecuteCommandIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    registry = build_default_command_registry()
    try:
        command = registry.get(command_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Command not found") from exc

    if command.name not in _EXECUTABLE_BUILTIN_COMMANDS:
        raise HTTPException(status_code=501, detail=f"Command {command.name!r} is not executable through this endpoint")

    result = await execute_tool(
        command.name,
        body.arguments,
        agent_id=agent_id,
        user_id=current_user.id,
        session_id=body.session_id,
    )
    parsed: Any
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = result
    else:
        parsed = result
    return {"ok": True, "command": command.name, "result": parsed}
