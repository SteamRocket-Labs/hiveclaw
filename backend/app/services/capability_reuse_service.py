"""Reuse already-installed capabilities before performing external installs."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import async_session
from app.models.skill import Skill
from app.models.tool import Tool
from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment


def _agent_dir(agent_id: uuid.UUID) -> Path:
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id)


async def reuse_existing_skill_for_agent(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    folder_name: str,
) -> dict | None:
    """Copy an existing registry skill into the agent workspace if already available."""
    async with async_session() as db:
        result = await db.execute(
            select(Skill)
            .where(
                Skill.folder_name == folder_name,
                or_(Skill.tenant_id == tenant_id, Skill.tenant_id.is_(None)),
            )
            .order_by(Skill.tenant_id.is_(None))
            .options(selectinload(Skill.files))
        )
        skill = result.scalar_one_or_none()
        if skill is None or not skill.files:
            return None

    base = _agent_dir(agent_id)
    skill_dir = base / "skills" / skill.folder_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for skill_file in skill.files:
        target = (skill_dir / skill_file.path).resolve()
        if not str(target).startswith(str(base.resolve())):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(skill_file.content, encoding="utf-8")
        written.append(skill_file.path)

    return {
        "status": "already_installed",
        "skill_id": str(skill.id),
        "skill_name": skill.name,
        "folder_name": skill.folder_name,
        "files_written": len(written),
        "files": written,
    }


async def _query_existing_mcp_tools(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    server_id: str,
) -> list[Tool]:
    clean_id = server_id.replace("/", "_").replace("@", "")
    tenant_filter = Tool.tenant_id == tenant_id if tenant_id else Tool.tenant_id.is_(None)
    result = await db.execute(
        select(Tool).where(
            Tool.type == "mcp",
            tenant_filter,
            or_(
                Tool.name.like(f"mcp_{clean_id}%"),
                Tool.name.like(f"mcp_{clean_id.split('_')[-1]}%"),
            ),
        )
    )
    return result.scalars().all()


async def reuse_existing_mcp_server_for_agent(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    server_id: str,
    config: dict | None = None,
) -> dict | None:
    """Reuse existing tenant MCP tools for a newly created agent."""
    async with async_session() as db:
        tools = await _query_existing_mcp_tools(db, tenant_id=tenant_id, server_id=server_id)
        if not tools:
            return None

        for tool in tools:
            await ensure_agent_tool_assignment(
                db,
                agent_id=agent_id,
                tool_id=tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent_id,
                config=config or {},
            )
        await db.commit()

    return {
        "status": "already_installed",
        "server_id": server_id,
        "tool_count": len(tools),
        "tools": [tool.display_name for tool in tools],
    }
