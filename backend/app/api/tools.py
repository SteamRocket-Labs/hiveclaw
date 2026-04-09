"""Legacy-compatible tools API surface for the current frontend."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment
from app.services.email_service import test_connection as test_email_connection
from app.services.mcp_client import MCPClient

router = APIRouter(tags=["tools"])


class ToolCreateIn(BaseModel):
    name: str
    display_name: str
    description: str = ""
    type: str = "builtin"
    category: str = "general"
    icon: str = "🔧"
    parameters_schema: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)
    config_schema: dict = Field(default_factory=dict)
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    mcp_tool_name: str | None = None
    enabled: bool = True
    is_default: bool = False


class ToolUpdateIn(BaseModel):
    enabled: bool | None = None
    config: dict | None = None


class AgentToolToggleIn(BaseModel):
    tool_id: str
    enabled: bool


class AgentToolsUpdateIn(BaseModel):
    tools: list[AgentToolToggleIn]


class CategoryConfigIn(BaseModel):
    config: dict = Field(default_factory=dict)


class McpTestIn(BaseModel):
    server_url: str
    api_key: str | None = None


class EmailTestIn(BaseModel):
    config: dict = Field(default_factory=dict)


async def _build_feishu_runtime_status(agent_id: uuid.UUID | None = None) -> dict:
    from app.services.agent_tool_domains.feishu_cli import _feishu_cli_available

    settings = get_settings()
    cli_enabled = bool(getattr(settings, "FEISHU_CLI_ENABLED", False))
    cli_bin = getattr(settings, "FEISHU_CLI_BIN", "lark-cli")
    cli_available = await _feishu_cli_available()

    payload = {
        "scope": "agent" if agent_id is not None else "global",
        "cli_enabled": cli_enabled,
        "cli_available": cli_available,
        "cli_bin": cli_bin,
    }

    if agent_id is None:
        payload["ok"] = cli_available or cli_enabled
        payload["docs_read_ready"] = cli_available
        payload["base_tasks_ready"] = cli_available
        if cli_available:
            payload["message"] = "Feishu CLI is ready. Docs/Wiki/Sheets/Base/Tasks can use lark-cli."
        elif cli_enabled:
            payload["message"] = "Feishu CLI is enabled but not authenticated. Run `lark-cli auth login` inside the cloud container."
        else:
            payload["message"] = "Feishu CLI is disabled. Enable it to unlock Base/Tasks office tooling in cloud deployments."
        return payload

    from app.services.agent_tools import _agent_has_feishu, _agent_has_feishu_cli_access, _agent_has_feishu_office_access

    channel_configured = await _agent_has_feishu(agent_id)
    office_access = await _agent_has_feishu_office_access(agent_id)
    cli_access = await _agent_has_feishu_cli_access()

    payload.update(
        {
            "channel_configured": channel_configured,
            "office_access": office_access,
            "docs_read_ready": office_access,
            "base_tasks_ready": office_access,
            "ok": channel_configured or office_access or cli_enabled or cli_available,
        }
    )
    if channel_configured:
        payload["message"] = "Feishu channel auth is ready. All office tools (Docs/Wiki/Sheets/Base/Tasks) can run."
    elif cli_access:
        payload["message"] = "lark-cli is ready. Feishu office tools can run even without a channel binding."
    elif cli_enabled:
        payload["message"] = "Feishu CLI is enabled but not authenticated. Channel auth is also unavailable for this agent."
    else:
        payload["message"] = "This agent has no Feishu channel auth. Configure it in Enterprise Settings → Channels."
    return payload


def _serialize_tool(tool: Tool, *, enabled: bool | None = None, config: dict | None = None) -> dict:
    return {
        "id": str(tool.id),
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "type": tool.type,
        "category": tool.category,
        "icon": tool.icon,
        "parameters_schema": tool.parameters_schema or {},
        "config": config if config is not None else (tool.config or {}),
        "config_schema": tool.config_schema or {},
        "mcp_server_url": tool.mcp_server_url,
        "mcp_server_name": tool.mcp_server_name,
        "mcp_tool_name": tool.mcp_tool_name,
        "enabled": tool.enabled if enabled is None else enabled,
        "is_default": tool.is_default,
        "tenant_id": str(tool.tenant_id) if tool.tenant_id else None,
    }


async def _resolve_tenant_scope(
    current_user: User,
    tenant_id: str | None,
) -> uuid.UUID | None:
    if tenant_id:
        parsed = uuid.UUID(tenant_id)
        if current_user.role != "platform_admin" and current_user.tenant_id != parsed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant access denied")
        return parsed
    return current_user.tenant_id


async def _require_manage_access(db: AsyncSession, current_user: User, agent_id: uuid.UUID) -> Agent:
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access required")
    return agent


async def _get_tenant_agent_ids(db: AsyncSession, tenant_id: uuid.UUID | None) -> list[uuid.UUID]:
    if not tenant_id:
        return []
    result = await db.execute(select(Agent.id).where(Agent.tenant_id == tenant_id))
    return [getattr(row, "id", row) for row in result.scalars().all()]


async def _get_agent_tool(db: AsyncSession, agent_id: uuid.UUID, tool_id: uuid.UUID) -> AgentTool | None:
    result = await db.execute(
        select(AgentTool).where(
            AgentTool.agent_id == agent_id,
            AgentTool.tool_id == tool_id,
        )
    )
    return result.scalar_one_or_none()


async def _upsert_tenant_tool_assignments(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    tool: Tool,
    *,
    enabled: bool | None = None,
    config: dict | None = None,
) -> None:
    for agent_id in await _get_tenant_agent_ids(db, tenant_id):
        await ensure_agent_tool_assignment(
            db,
            agent_id=agent_id,
            tool_id=tool.id,
            enabled=tool.enabled if enabled is None else enabled,
            config=config if config is not None else None,
            source="system",
            merge_config=False,
        )


async def _serialize_tool_for_tenant(db: AsyncSession, tool: Tool, tenant_id: uuid.UUID | None) -> dict:
    if not tenant_id:
        return _serialize_tool(tool)

    agent_ids = await _get_tenant_agent_ids(db, tenant_id)
    assignment = None
    for agent_id in agent_ids:
        assignment = await _get_agent_tool(db, agent_id, tool.id)
        if assignment is not None:
            break

    effective_config = {**(tool.config or {})}
    if assignment and assignment.config:
        effective_config.update(assignment.config)
    effective_enabled = assignment.enabled if assignment is not None else tool.enabled
    return _serialize_tool(tool, enabled=effective_enabled, config=effective_config)


@router.get("/tools")
async def list_tools(
    tenant_id: str | None = Query(None),
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    scope_tenant_id = await _resolve_tenant_scope(current_user, tenant_id)
    stmt = select(Tool)
    if scope_tenant_id:
        # Tenant-scoped tools + platform built-in tools (tenant_id IS NULL, non-MCP).
        # MCP tools must match tenant_id exactly — prevents cross-tenant leakage.
        stmt = stmt.where(
            or_(
                Tool.tenant_id == scope_tenant_id,
                and_(Tool.tenant_id.is_(None), Tool.type != "mcp"),
            )
        )
    stmt = stmt.order_by(Tool.category.asc(), Tool.display_name.asc())
    result = await db.execute(stmt)
    tools = result.scalars().all()

    # Dedup MCP tools from different import paths that represent the same
    # server+tool combination.  Key on structural identity (server_name,
    # tool_name) so two different servers that expose identically named tools
    # are NOT incorrectly merged.  Falls back to display_name when the MCP
    # metadata fields are NULL (legacy rows).
    seen_mcp: set[tuple[str | None, str | None]] = set()
    deduped: list[Tool] = []
    for tool in tools:
        if tool.type == "mcp":
            key = (tool.mcp_server_name, tool.mcp_tool_name) if tool.mcp_tool_name else (tool.display_name, None)
            if key in seen_mcp:
                continue
            seen_mcp.add(key)
        deduped.append(tool)

    return [await _serialize_tool_for_tenant(db, tool, scope_tenant_id) for tool in deduped]


@router.get("/tools/runtime/feishu-status")
async def get_feishu_runtime_status(
    current_user: User = Depends(get_current_admin),
):
    _ = current_user
    return await _build_feishu_runtime_status()


@router.get("/tools/agent-installed")
async def list_agent_installed_tools(
    tenant_id: str | None = Query(None),
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    scope_tenant_id = await _resolve_tenant_scope(current_user, tenant_id)
    if not scope_tenant_id:
        return []
    result = await db.execute(
        select(AgentTool, Tool, Agent)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .join(Agent, Agent.id == AgentTool.agent_id)
        .where(
            Agent.tenant_id == scope_tenant_id,
            AgentTool.source == "user_installed",
        )
        .order_by(AgentTool.created_at.desc())
    )
    rows = result.all()
    payload = []
    for agent_tool, tool, agent in rows:
        payload.append(
            {
                "agent_tool_id": str(agent_tool.id),
                "tool_id": str(tool.id),
                "tool_display_name": tool.display_name,
                "tool_name": tool.name,
                "mcp_server_name": tool.mcp_server_name,
                "installed_by_agent_name": agent.name,
                "installed_at": agent_tool.created_at.isoformat() if agent_tool.created_at else None,
            }
        )
    return payload


@router.post("/tools/dedup-mcp")
async def dedup_mcp_tools(
    tenant_id: str | None = Query(None),
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Merge duplicate MCP tools that share (display_name, tenant_id).

    Keeps the oldest Tool record, re-points AgentTool rows from duplicates
    to the keeper, then deletes the duplicate Tool rows.
    """
    scope_tenant_id = await _resolve_tenant_scope(current_user, tenant_id)
    if not scope_tenant_id:
        return {"merged": 0}

    # Find all MCP tools for this tenant
    result = await db.execute(
        select(Tool).where(Tool.tenant_id == scope_tenant_id, Tool.type == "mcp").order_by(Tool.created_at.asc())
    )
    all_mcp = result.scalars().all()

    # Group by structural identity (server_name, tool_name) — keeps first (oldest).
    # Falls back to display_name for legacy rows missing mcp_tool_name.
    groups: dict[tuple[str | None, str | None], list[Tool]] = {}
    for tool in all_mcp:
        key = (tool.mcp_server_name, tool.mcp_tool_name) if tool.mcp_tool_name else (tool.display_name, None)
        groups.setdefault(key, []).append(tool)

    merged = 0
    for _key, tools in groups.items():
        if len(tools) <= 1:
            continue
        keeper = tools[0]
        for dup in tools[1:]:
            # Move AgentTool references from dup → keeper (skip if already exists)
            at_result = await db.execute(select(AgentTool).where(AgentTool.tool_id == dup.id))
            for agent_tool in at_result.scalars().all():
                existing = await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_tool.agent_id,
                        AgentTool.tool_id == keeper.id,
                    )
                )
                if not existing.scalar_one_or_none():
                    agent_tool.tool_id = keeper.id
                else:
                    await db.delete(agent_tool)
            await db.delete(dup)
            merged += 1

    if merged:
        await db.commit()
    return {"merged": merged}


@router.post("/tools")
async def create_tool(
    data: ToolCreateIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    tool = Tool(
        id=uuid.uuid4(),
        name=data.name,
        display_name=data.display_name,
        description=data.description,
        type=data.type,
        category=data.category,
        icon=data.icon,
        parameters_schema=data.parameters_schema,
        config=data.config,
        config_schema=data.config_schema,
        mcp_server_url=data.mcp_server_url,
        mcp_server_name=data.mcp_server_name,
        mcp_tool_name=data.mcp_tool_name,
        enabled=data.enabled,
        is_default=data.is_default,
        tenant_id=current_user.tenant_id,
    )
    db.add(tool)
    await db.flush()

    for agent_id in await _get_tenant_agent_ids(db, current_user.tenant_id):
        await ensure_agent_tool_assignment(
            db,
            agent_id=agent_id,
            tool_id=tool.id,
            enabled=data.enabled,
            config=data.config or {},
            source="system",
            merge_config=False,
        )

    await db.commit()
    return _serialize_tool(tool)


@router.put("/tools/{tool_id}")
async def update_global_tool(
    tool_id: uuid.UUID,
    data: ToolUpdateIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")

    if tool.tenant_id and current_user.role != "platform_admin" and tool.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tool access denied")

    if tool.tenant_id is None and current_user.role != "platform_admin":
        await _upsert_tenant_tool_assignments(
            db,
            current_user.tenant_id,
            tool,
            enabled=data.enabled,
            config=data.config,
        )
    else:
        if data.enabled is not None:
            tool.enabled = data.enabled
        if data.config is not None:
            tool.config = data.config

    await db.commit()
    return await _serialize_tool_for_tenant(db, tool, current_user.tenant_id)


@router.delete("/tools/{tool_id}")
async def delete_global_tool(
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")

    if tool.tenant_id and current_user.role != "platform_admin" and tool.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tool access denied")

    if tool.tenant_id is None and current_user.role != "platform_admin":
        agent_ids = await _get_tenant_agent_ids(db, current_user.tenant_id)
        await db.execute(delete(AgentTool).where(AgentTool.tool_id == tool.id, AgentTool.agent_id.in_(agent_ids)))
    else:
        await db.execute(delete(AgentTool).where(AgentTool.tool_id == tool.id))
        await db.delete(tool)

    await db.commit()
    return {"status": "deleted", "tool_id": str(tool_id)}


@router.post("/tools/test-mcp")
async def test_mcp_server(
    data: McpTestIn,
    current_user: User = Depends(get_current_admin),
):
    _ = current_user
    client = MCPClient(data.server_url, api_key=data.api_key)
    try:
        tools = await client.list_tools()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"ok": True, "tools": tools}


@router.post("/tools/test-email")
async def test_email_config(
    data: EmailTestIn,
    current_user: User = Depends(get_current_admin),
):
    _ = current_user
    return await test_email_connection(data.config)


@router.get("/tools/agents/{agent_id}/with-config")
async def list_agent_tools_with_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentTool, Tool)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(AgentTool.agent_id == agent_id)
        .order_by(Tool.category.asc(), Tool.display_name.asc())
    )
    rows = result.all()
    return [
        {
            **_serialize_tool(
                tool, enabled=agent_tool.enabled, config={**(tool.config or {}), **(agent_tool.config or {})}
            ),
            "agent_tool_id": str(agent_tool.id),
            "source": agent_tool.source,
            "global_config": tool.config or {},
            "agent_config": agent_tool.config or {},
        }
        for agent_tool, tool in rows
    ]


@router.get("/tools/agents/{agent_id}")
async def list_agent_tools(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_agent_tools_with_config(agent_id=agent_id, current_user=current_user, db=db)


@router.put("/tools/agents/{agent_id}")
async def update_agent_tools(
    agent_id: uuid.UUID,
    data: AgentToolsUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_access(db, current_user, agent_id)
    for update_item in data.tools:
        assignment = await _get_agent_tool(db, agent_id, uuid.UUID(update_item.tool_id))
        if assignment:
            assignment.enabled = update_item.enabled
    await db.commit()
    return {"ok": True}


@router.get("/tools/agents/{agent_id}/category-config/{category}")
async def get_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentTool, Tool)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(AgentTool.agent_id == agent_id, Tool.category == category)
        .order_by(Tool.display_name.asc())
    )
    rows = result.all()
    if not rows:
        return {"config": {}}
    agent_tool, tool = rows[0]
    return {"config": {**(tool.config or {}), **(agent_tool.config or {})}}


@router.get("/tools/agents/{agent_id}/runtime/feishu-status")
async def get_agent_feishu_runtime_status(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ = await _require_manage_access(db, current_user, agent_id)
    return await _build_feishu_runtime_status(agent_id)


@router.put("/tools/agents/{agent_id}/category-config/{category}")
async def update_category_config(
    agent_id: uuid.UUID,
    category: str,
    data: CategoryConfigIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentTool)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(AgentTool.agent_id == agent_id, Tool.category == category)
    )
    assignments = result.scalars().all()
    for assignment in assignments:
        assignment.config = data.config
    await db.commit()
    return {"ok": True, "config": data.config}


@router.post("/tools/agents/{agent_id}/category-config/{category}/test")
async def test_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ = await _require_manage_access(db, current_user, agent_id)
    if category == "feishu":
        return await _build_feishu_runtime_status(agent_id)
    config_payload = await get_category_config(agent_id=agent_id, category=category, current_user=current_user, db=db)
    config = config_payload.get("config", {})
    if category == "email":
        return await test_email_connection(config)
    if category == "agentbay":
        ok = bool(config.get("api_key"))
        return {"ok": ok, "message": "AgentBay configured" if ok else "AgentBay API key is required"}
    return {"ok": True, "message": f"No validator registered for category '{category}'"}


@router.put("/tools/agents/{agent_id}/tool-config/{tool_id}")
async def update_tool_config(
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    data: CategoryConfigIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_access(db, current_user, agent_id)
    assignment, _ = await ensure_agent_tool_assignment(
        db,
        agent_id=agent_id,
        tool_id=tool_id,
        enabled=True,
        config=data.config,
        source="system",
        merge_config=False,
    )
    assignment.config = data.config
    await db.commit()
    return {"ok": True}


@router.get("/tools/agent-tool/{tool_id}")
async def get_tool_detail(
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ = current_user
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    return _serialize_tool(tool)


@router.delete("/tools/agent-tool/{agent_tool_id}")
async def remove_agent_tool(
    agent_tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AgentTool).where(AgentTool.id == agent_tool_id))
    agent_tool = result.scalar_one_or_none()
    if not agent_tool:
        return {"status": "deleted", "agent_tool_id": str(agent_tool_id)}

    await _require_manage_access(db, current_user, agent_tool.agent_id)
    await db.delete(agent_tool)
    remaining = await db.execute(select(AgentTool).where(AgentTool.tool_id == agent_tool.tool_id))
    if not remaining.scalar_one_or_none():
        tool_row = await db.execute(select(Tool).where(Tool.id == agent_tool.tool_id))
        tool = tool_row.scalar_one_or_none()
        if tool and tool.type == "mcp":
            await db.delete(tool)
    await db.commit()
    return {"status": "deleted", "agent_tool_id": str(agent_tool_id)}
