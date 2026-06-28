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
from app.models.channel_config import ChannelConfig
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.tenant_setting import TenantSetting
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment
from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token_status
from app.services.email_service import test_connection as test_email_connection
from app.services.governance_capability_taxonomy import capability_descriptor_for_tool, is_agent_base_tool
from app.services.mcp_client import MCPClient
from app.services.tool_config_service import (
    encrypt_tool_config_secrets,
    mask_tool_config_secrets,
    merge_tool_config_secrets,
    resolve_tool_config_for_tenant_display,
    update_tenant_tool_config,
)
from app.services.tool_visibility import is_tool_allowed_for_agent

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


async def _tenant_has_feishu_channel_config(db: AsyncSession | None, tenant_id: uuid.UUID | None) -> bool:
    if db is None or tenant_id is None:
        return False
    try:
        result = await db.execute(
            select(TenantChannelConfig).where(
                TenantChannelConfig.tenant_id == tenant_id,
                TenantChannelConfig.channel_type == "feishu",
                TenantChannelConfig.is_active.is_(True),
            )
        )
    except AssertionError:
        return False
    config = result.scalar_one_or_none()
    return bool(config and config.app_id and config.app_secret)


async def _tenant_has_feishu_provider_config(db: AsyncSession | None, tenant_id: uuid.UUID | None) -> bool:
    if db is None or tenant_id is None:
        return False
    try:
        result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == tenant_id,
                TenantSetting.key == "feishu_org_sync",
            )
        )
    except AssertionError:
        return False
    setting = result.scalar_one_or_none()
    value = getattr(setting, "value", {}) or {}
    return bool(value.get("app_id") and value.get("app_secret"))


async def _build_feishu_runtime_status(
    agent_id: uuid.UUID | None = None,
    *,
    db: AsyncSession | None = None,
    tenant_id: uuid.UUID | None = None,
) -> dict:
    from app.services.agent_tool_domains.feishu_cli import _feishu_cli_available
    from app.services.feishu_service import _HAS_LARK

    settings = get_settings()
    cli_enabled = bool(getattr(settings, "FEISHU_CLI_ENABLED", False))
    cli_bin = getattr(settings, "FEISHU_CLI_BIN", "lark-cli")
    cli_available = await _feishu_cli_available()
    provider_configured = await _tenant_has_feishu_provider_config(db, tenant_id)
    tenant_channel_configured = await _tenant_has_feishu_channel_config(db, tenant_id)
    tenant_auth_ready = tenant_channel_configured or provider_configured

    payload = {
        "scope": "agent" if agent_id is not None else "global",
        "cli_enabled": cli_enabled,
        "cli_available": cli_available,
        "cli_bin": cli_bin,
        "tenant_channel_configured": tenant_auth_ready,
        "cardkit_dependency_ready": False,
        "cardkit_verified": None,
        "cardkit_last_error": None,
        "cardkit_probe_supported": True,
        "cardkit_ready": False,
    }

    if agent_id is None:
        docs_ready = cli_available or tenant_auth_ready
        payload["docs_read_ready"] = docs_ready
        payload["base_tasks_ready"] = docs_ready
        payload["cardkit_dependency_ready"] = _HAS_LARK and tenant_auth_ready
        payload["cardkit_ready"] = payload["cardkit_dependency_ready"]
        payload["ok"] = docs_ready or cli_enabled
        if tenant_auth_ready and cli_available:
            payload["message"] = (
                "Tenant Feishu auth and CLI auth are both ready. CardKit dependencies are present and office tools can run."
            )
        elif tenant_auth_ready:
            payload["message"] = (
                "Tenant Feishu auth is ready. CardKit dependencies are present and OpenAPI-backed office tools can run."
            )
        elif cli_available:
            payload["message"] = "Feishu CLI is ready. Docs/Wiki/Sheets/Base/Tasks can use lark-cli."
        elif cli_enabled:
            payload["message"] = (
                "Feishu CLI is enabled but not authenticated. Run `lark-cli auth login` inside the cloud container."
            )
        else:
            payload["message"] = (
                "Feishu CLI is disabled. Enable it to unlock Base/Tasks office tooling in cloud deployments."
            )
        return payload

    from app.services.agent_tools import (
        _agent_has_feishu,
        _agent_has_feishu_cli_access,
        _agent_has_feishu_office_access,
    )

    channel_configured = await _agent_has_feishu(agent_id)
    office_access = await _agent_has_feishu_office_access(agent_id)
    cli_access = await _agent_has_feishu_cli_access()
    channel_auth_valid: bool | None = None
    channel_auth_error: str | None = None
    if channel_configured:
        token_status = await _get_feishu_token_status(agent_id)
        channel_auth_valid = bool(token_status.get("ok"))
        if not channel_auth_valid:
            channel_auth_error = token_status.get("message") or "Feishu channel authentication failed."
            office_access = False

    docs_ready = office_access or tenant_auth_ready
    base_ready = office_access or tenant_auth_ready

    payload.update(
        {
            "channel_configured": channel_configured,
            "channel_auth_valid": channel_auth_valid,
            "channel_auth_error": channel_auth_error,
            "office_access": office_access,
            "docs_read_ready": docs_ready,
            "base_tasks_ready": base_ready,
            "cardkit_dependency_ready": _HAS_LARK and (channel_auth_valid or tenant_auth_ready),
            "ok": (channel_auth_valid if channel_configured else False) or docs_ready or cli_enabled or cli_available,
        }
    )
    if channel_configured and channel_auth_valid is False:
        payload["ok"] = False
        payload["message"] = (
            "Feishu channel config exists, but authentication failed: "
            f"{channel_auth_error}. Update the app_secret and retest."
        )
        payload["cardkit_ready"] = payload["cardkit_dependency_ready"]
        return payload
    payload["cardkit_ready"] = payload["cardkit_dependency_ready"]
    if channel_configured:
        payload["message"] = (
            "Feishu channel auth is ready. CardKit dependencies and office tools are available for this agent."
        )
    elif tenant_auth_ready:
        payload["message"] = (
            "Tenant Feishu auth is ready. Tenant webhook routing is available and CardKit dependencies are present."
        )
    elif cli_access:
        payload["message"] = "lark-cli is ready. Feishu office tools can run even without a channel binding."
    elif cli_enabled:
        payload["message"] = (
            "Feishu CLI is enabled but not authenticated. Channel auth is also unavailable for this agent."
        )
    else:
        payload["message"] = "This agent has no Feishu channel auth. Configure it in Enterprise Settings → Channels."
    return payload


async def _get_feishu_probe_credentials(
    *,
    db: AsyncSession | None,
    agent_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
) -> tuple[str, str, dict] | None:
    if db is None:
        return None

    if agent_id is not None:
        try:
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                    ChannelConfig.is_configured.is_(True),
                )
            )
        except AssertionError:
            result = None
        config = result.scalar_one_or_none() if result else None
        if config and config.app_id and config.app_secret:
            return config.app_id, config.app_secret, config.extra_config

    if tenant_id is not None:
        try:
            result = await db.execute(
                select(TenantChannelConfig).where(
                    TenantChannelConfig.tenant_id == tenant_id,
                    TenantChannelConfig.channel_type == "feishu",
                    TenantChannelConfig.is_active.is_(True),
                )
            )
        except AssertionError:
            result = None
        config = result.scalar_one_or_none() if result else None
        if config and config.app_id and config.app_secret:
            return config.app_id, config.app_secret, config.extra_config

        try:
            result = await db.execute(
                select(TenantSetting).where(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.key == "feishu_org_sync",
                )
            )
        except AssertionError:
            result = None
        setting = result.scalar_one_or_none() if result else None
        value = getattr(setting, "value", {}) or {}
        if value.get("app_id") and value.get("app_secret"):
            return value["app_id"], value["app_secret"], value

    return None


async def _probe_feishu_cardkit_status(
    *,
    agent_id: uuid.UUID,
    db: AsyncSession | None,
    tenant_id: uuid.UUID | None,
) -> dict:
    from app.services.feishu_service import _HAS_LARK, feishu_service

    if not _HAS_LARK:
        return {
            "cardkit_verified": False,
            "cardkit_last_error": "lark-oapi package is not installed.",
            "cardkit_probe_supported": True,
        }

    creds = await _get_feishu_probe_credentials(db=db, agent_id=agent_id, tenant_id=tenant_id)
    if not creds:
        return {
            "cardkit_verified": False,
            "cardkit_last_error": "Feishu auth is not configured for CardKit probe.",
            "cardkit_probe_supported": True,
        }

    app_id, app_secret, extra_config = creds
    probe_card = {
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "Hive CardKit Probe"}},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": "This entity is created only to verify CardKit access."},
            }
        ],
    }

    try:
        await feishu_service.create_card_entity(app_id, app_secret, probe_card, extra_config=extra_config)
    except Exception as exc:
        return {
            "cardkit_verified": False,
            "cardkit_last_error": str(exc),
            "cardkit_probe_supported": True,
        }

    return {
        "cardkit_verified": True,
        "cardkit_last_error": None,
        "cardkit_probe_supported": True,
    }


def _serialize_tool(tool: Tool, *, enabled: bool | None = None, config: dict | None = None) -> dict:
    taxonomy = capability_descriptor_for_tool(str(tool.name))
    governance_taxonomy = None
    if taxonomy is not None:
        governance_taxonomy = {
            "name": taxonomy.name,
            "layer": taxonomy.layer,
            "l2_visible": taxonomy.l2_visible,
            "enterprise_toggleable": taxonomy.enterprise_toggleable,
            "default_enabled": taxonomy.default_enabled,
            "requires_local_bridge": taxonomy.requires_local_bridge,
            "source": taxonomy.source,
        }
    elif tool.type == "mcp":
        governance_taxonomy = {
            "name": tool.mcp_server_name or "mcp_server",
            "layer": "external_extension",
            "l2_visible": True,
            "enterprise_toggleable": True,
            "default_enabled": False,
            "requires_local_bridge": False,
            "source": "mcp",
        }
    elif tool.type == "custom_api" or str(tool.name).startswith("custom_api__"):
        governance_taxonomy = {
            "name": "custom_api_connector",
            "layer": "external_extension",
            "l2_visible": True,
            "enterprise_toggleable": True,
            "default_enabled": False,
            "requires_local_bridge": False,
            "source": "custom_api",
        }
    return {
        "id": str(tool.id),
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "type": tool.type,
        "category": tool.category,
        "icon": tool.icon,
        "parameters_schema": tool.parameters_schema or {},
        "config": mask_tool_config_secrets(
            config if config is not None else (tool.config or {}), tool.config_schema or {}
        ),
        "config_schema": tool.config_schema or {},
        "mcp_server_url": tool.mcp_server_url,
        "mcp_server_name": tool.mcp_server_name,
        "mcp_tool_name": tool.mcp_tool_name,
        "enabled": tool.enabled if enabled is None else enabled,
        "is_default": tool.is_default,
        "tenant_id": str(tool.tenant_id) if tool.tenant_id else None,
        "governance_taxonomy": governance_taxonomy,
    }


async def _resolve_tenant_scope(
    db: AsyncSession,
    current_user: User,
    tenant_id: str | None,
) -> uuid.UUID | None:
    from app.database import pin_rls_tenant_context

    if tenant_id:
        parsed = uuid.UUID(tenant_id)
        if current_user.role != "platform_admin" and current_user.tenant_id != parsed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant access denied")
        await pin_rls_tenant_context(db, parsed)
        return parsed
    await pin_rls_tenant_context(db, current_user.tenant_id)
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


def _tool_visible_to_agent_tenant(tool: Tool, tenant_id: uuid.UUID | None) -> bool:
    if tool.tenant_id is None:
        return tool.type != "mcp"
    return bool(tenant_id and tool.tenant_id == tenant_id)


async def _get_visible_agent_tool(db: AsyncSession, agent: Agent, tool_id: uuid.UUID) -> Tool | None:
    result = await db.execute(select(Tool).where(Tool.id == tool_id, Tool.enabled.is_(True)))
    tool = result.scalar_one_or_none()
    if (
        not tool
        or not _tool_visible_to_agent_tenant(tool, agent.tenant_id)
        or not is_tool_allowed_for_agent(tool, agent)
    ):
        return None
    return tool


async def _get_agent_skill_declared_tool_names(agent_id: uuid.UUID) -> set[str]:
    """Return tools explicitly declared by skills installed in this agent workspace."""
    try:
        from app.services.pack_service import _load_agent_skill_declared_packs

        declared_packs = await _load_agent_skill_declared_packs(agent_id)
    except Exception:
        return set()

    tool_names: set[str] = set()
    for pack in declared_packs:
        tools = pack.get("tools") if isinstance(pack, dict) else None
        if isinstance(tools, list):
            tool_names.update(str(name) for name in tools if name)
    return tool_names


def _serialize_agent_tool_row(tool: Tool, agent_tool: AgentTool | None) -> dict:
    agent_config = agent_tool.config if agent_tool else {}
    return {
        **_serialize_tool(
            tool,
            enabled=agent_tool.enabled if agent_tool else bool(tool.is_default),
            config={**(tool.config or {}), **(agent_config or {})},
        ),
        "agent_tool_id": str(agent_tool.id) if agent_tool else None,
        "source": agent_tool.source if agent_tool else "system",
        "global_config": mask_tool_config_secrets(tool.config or {}, tool.config_schema or {}),
        "agent_config": mask_tool_config_secrets(agent_config or {}, tool.config_schema or {}),
    }


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

    effective_config, effective_enabled = await resolve_tool_config_for_tenant_display(db, tool, tenant_id)
    return _serialize_tool(tool, enabled=effective_enabled, config=effective_config)


@router.get("/tools")
async def list_tools(
    tenant_id: str | None = Query(None),
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    scope_tenant_id = await _resolve_tenant_scope(db, current_user, tenant_id)
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
    db: AsyncSession = Depends(get_db),
):
    return await _build_feishu_runtime_status(db=db, tenant_id=current_user.tenant_id)


@router.get("/tools/agent-installed")
async def list_agent_installed_tools(
    tenant_id: str | None = Query(None),
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    scope_tenant_id = await _resolve_tenant_scope(db, current_user, tenant_id)
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
    scope_tenant_id = await _resolve_tenant_scope(db, current_user, tenant_id)
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
        mcp_tool_name = getattr(tool, "mcp_tool_name", None)
        key = (getattr(tool, "mcp_server_name", None), mcp_tool_name) if mcp_tool_name else (tool.display_name, None)
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
            tenant_id=current_user.tenant_id,
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

    if tool.tenant_id is None:
        if data.enabled is False and is_agent_base_tool(str(tool.name)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="agent_base_capability_not_toggleable",
            )
        # Builtin tool (shared across tenants): write to TenantToolConfig, never modify Tool.config.
        # This applies to ALL users including platform_admin — each tenant's config is isolated.
        ttc = await update_tenant_tool_config(
            db,
            current_user.tenant_id,
            tool.id,
            config=data.config,
            enabled=data.enabled,
            config_schema=tool.config_schema or {},
        )
        # Propagate the reconciled (unmasked) config to per-agent assignments so
        # a masked round-trip never writes the sentinel into agent_tools.
        await _upsert_tenant_tool_assignments(
            db,
            current_user.tenant_id,
            tool,
            enabled=data.enabled,
            config=(ttc.config if data.config is not None else None),
        )
    else:
        # Tenant-scoped tool (e.g. MCP): owning tenant can modify directly
        if data.enabled is not None:
            tool.enabled = data.enabled
        if data.config is not None:
            merged = merge_tool_config_secrets(data.config, tool.config or {}, tool.config_schema or {})
            tool.config = encrypt_tool_config_secrets(merged, tool.config_schema or {})

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

    if tool.tenant_id is None:
        if is_agent_base_tool(str(tool.name)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="agent_base_capability_not_toggleable",
            )
        # Builtin tool: NEVER delete the global Tool row. Remove this tenant's
        # assignments and mark TenantToolConfig as disabled.
        agent_ids = await _get_tenant_agent_ids(db, current_user.tenant_id)
        if agent_ids:
            await db.execute(delete(AgentTool).where(AgentTool.tool_id == tool.id, AgentTool.agent_id.in_(agent_ids)))
        await update_tenant_tool_config(db, current_user.tenant_id, tool.id, enabled=False)
    else:
        # Tenant-scoped tool (e.g. MCP): owning tenant can delete
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
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    assignments_result = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
    assignments = {agent_tool.tool_id: agent_tool for agent_tool in assignments_result.scalars().all()}
    skill_declared_tool_names = await _get_agent_skill_declared_tool_names(agent_id)

    tools_stmt = select(Tool).where(Tool.enabled.is_(True))
    if agent.tenant_id:
        tools_stmt = tools_stmt.where(
            or_(
                Tool.tenant_id == agent.tenant_id,
                and_(Tool.tenant_id.is_(None), Tool.type != "mcp"),
            )
        )
    else:
        tools_stmt = tools_stmt.where(Tool.tenant_id.is_(None), Tool.type != "mcp")
    tools_result = await db.execute(tools_stmt.order_by(Tool.category.asc(), Tool.display_name.asc()))
    tool_rows = tools_result.scalars().all()
    tool_rows = [
        tool
        for tool in tool_rows
        if is_tool_allowed_for_agent(tool, agent)
        and (
            tool.is_default
            or tool.id in assignments
            or tool.name in skill_declared_tool_names
            or is_agent_base_tool(str(tool.name))
        )
    ]
    return [_serialize_agent_tool_row(tool, assignments.get(tool.id)) for tool in tool_rows]


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
    agent = await _require_manage_access(db, current_user, agent_id)
    for update_item in data.tools:
        tool_id = uuid.UUID(update_item.tool_id)
        assignment = await _get_agent_tool(db, agent_id, tool_id)
        if assignment:
            tool = await _get_visible_agent_tool(db, agent, assignment.tool_id)
            if not tool:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
            if is_agent_base_tool(str(tool.name)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="agent_base_capability_not_toggleable",
                )
            assignment.enabled = update_item.enabled
            continue
        tool = await _get_visible_agent_tool(db, agent, tool_id)
        if not tool:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
        if is_agent_base_tool(str(tool.name)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="agent_base_capability_not_toggleable",
            )
        await ensure_agent_tool_assignment(
            db,
            agent_id=agent_id,
            tool_id=tool.id,
            enabled=update_item.enabled,
            source="system",
        )
    await db.commit()
    return {"ok": True}


@router.get("/tools/agents/{agent_id}/category-config/{category}")
async def get_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _require_manage_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentTool, Tool)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(AgentTool.agent_id == agent_id, Tool.category == category)
        .order_by(Tool.display_name.asc())
    )
    rows = [(agent_tool, tool) for agent_tool, tool in result.all() if is_tool_allowed_for_agent(tool, agent)]
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
    agent = await _require_manage_access(db, current_user, agent_id)
    return await _build_feishu_runtime_status(
        agent_id,
        db=db,
        tenant_id=getattr(agent, "tenant_id", current_user.tenant_id),
    )


@router.put("/tools/agents/{agent_id}/category-config/{category}")
async def update_category_config(
    agent_id: uuid.UUID,
    category: str,
    data: CategoryConfigIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _require_manage_access(db, current_user, agent_id)
    rows_result = await db.execute(
        select(AgentTool, Tool)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(AgentTool.agent_id == agent_id, Tool.category == category)
    )
    assignments = [agent_tool for agent_tool, tool in rows_result.all() if is_tool_allowed_for_agent(tool, agent)]
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
    agent = await _require_manage_access(db, current_user, agent_id)
    if category == "feishu":
        tenant_id = getattr(agent, "tenant_id", current_user.tenant_id)
        payload = await _build_feishu_runtime_status(agent_id, db=db, tenant_id=tenant_id)
        payload.update(await _probe_feishu_cardkit_status(agent_id=agent_id, db=db, tenant_id=tenant_id))
        return payload
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
    agent = await _require_manage_access(db, current_user, agent_id)
    if not await _get_visible_agent_tool(db, agent, tool_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
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
