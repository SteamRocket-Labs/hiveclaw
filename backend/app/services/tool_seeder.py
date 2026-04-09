"""Seed builtin tools into the database on startup."""

from __future__ import annotations

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.tool import Tool
from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment
from app.tools.collector import collect_tools


def _load_builtin_tools() -> list[dict]:
    """Derive builtin tool seeds from the decorator-based tool surface."""
    return collect_tools().seed_list


BUILTIN_TOOLS = _load_builtin_tools()


def _current_builtin_tool_names() -> set[str]:
    return {tool["name"] for tool in BUILTIN_TOOLS}


def _names_of_stale_builtin_tools(existing_names: set[str]) -> set[str]:
    return {name for name in existing_names if name not in _current_builtin_tool_names()}


async def seed_builtin_tools():
    """Insert or update builtin tools in the database."""
    from app.models.agent import Agent

    async with async_session() as db:
        new_tool_ids = []
        for t in BUILTIN_TOOLS:
            result = await db.execute(select(Tool).where(Tool.name == t["name"]))
            existing = result.scalar_one_or_none()
            if not existing:
                tool = Tool(
                    name=t["name"],
                    display_name=t["display_name"],
                    description=t["description"],
                    type="builtin",
                    category=t["category"],
                    icon=t["icon"],
                    is_default=t["is_default"],
                    parameters_schema=t["parameters_schema"],
                    config=t.get("config", {}),
                    config_schema=t.get("config_schema", {}),
                )
                db.add(tool)
                await db.flush()
                if t["is_default"]:
                    new_tool_ids.append(tool.id)
                logger.info(f"[ToolSeeder] Created builtin tool: {t['name']}")
            else:
                updated_fields = []
                if existing.category != t["category"]:
                    existing.category = t["category"]
                    updated_fields.append("category")
                if existing.description != t["description"]:
                    existing.description = t["description"]
                    updated_fields.append("description")
                if existing.display_name != t["display_name"]:
                    existing.display_name = t["display_name"]
                    updated_fields.append("display_name")
                if existing.icon != t["icon"]:
                    existing.icon = t["icon"]
                    updated_fields.append("icon")
                if existing.parameters_schema != t["parameters_schema"]:
                    existing.parameters_schema = t["parameters_schema"]
                    updated_fields.append("parameters_schema")
                if existing.config_schema != t.get("config_schema", {}):
                    existing.config_schema = t.get("config_schema", {})
                    updated_fields.append("config_schema")
                # NEVER overwrite user-configured config (API keys etc.)
                # Only initialize config if it's empty/null
                if not existing.config and t.get("config"):
                    existing.config = t.get("config", {})
                    updated_fields.append("config")
                if existing.is_default != t["is_default"]:
                    existing.is_default = t["is_default"]
                    updated_fields.append("is_default")
                if updated_fields:
                    logger.info(f"[ToolSeeder] Updated {', '.join(updated_fields)}: {t['name']}")

        if new_tool_ids:
            agents_result = await db.execute(select(Agent.id))
            agent_ids = [row[0] for row in agents_result.fetchall()]
            for agent_id in agent_ids:
                for tool_id in new_tool_ids:
                    await ensure_agent_tool_assignment(
                        db,
                        agent_id=agent_id,
                        tool_id=tool_id,
                        enabled=True,
                    )
            logger.info(f"[ToolSeeder] Auto-assigned {len(new_tool_ids)} new tools to {len(agent_ids)} agents")

        existing_builtin_rows = await db.execute(
            select(Tool).where(Tool.type == "builtin", Tool.tenant_id.is_(None))
        )
        stale_builtin_names = _names_of_stale_builtin_tools({tool.name for tool in existing_builtin_rows.scalars().all()})
        if stale_builtin_names:
            stale_rows = await db.execute(
                select(Tool).where(Tool.type == "builtin", Tool.tenant_id.is_(None), Tool.name.in_(stale_builtin_names))
            )
            for obsolete in stale_rows.scalars().all():
                await db.delete(obsolete)
                logger.info(f"[ToolSeeder] Removed stale builtin tool: {obsolete.name}")

        await db.commit()
        logger.info("[ToolSeeder] Builtin tools seeded")


_HR_ONLY_TOOLS = {"create_digital_employee"}


async def assign_default_tools_to_agent(db, agent_id) -> int:
    """Assign all is_default=True tools to a newly created agent.

    Call this after creating an agent to ensure it has all platform tools.
    Excludes HR-only tools (create_digital_employee) from normal agents.
    Returns the number of tools assigned.
    """
    result = await db.execute(select(Tool).where(Tool.is_default.is_(True)))
    count = 0
    for tool in result.scalars():
        if tool.name in _HR_ONLY_TOOLS:
            continue
        _, created = await ensure_agent_tool_assignment(
            db,
            agent_id=agent_id,
            tool_id=tool.id,
            enabled=True,
        )
        if created:
            count += 1
    return count


ATLASSIAN_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

ATLASSIAN_ROVO_CONFIG_TOOL = {
    "name": "atlassian_rovo",
    "display_name": "Atlassian Rovo (Jira / Confluence / Compass)",
    "description": (
        "Connect to Atlassian Rovo MCP Server to access Jira, Confluence, and Compass. "
        "Configure your API key to enable Jira issue management, Confluence page creation, "
        "and Compass component queries."
    ),
    "category": "atlassian",
    "icon": "🔷",
    "is_default": False,
    "parameters_schema": {"type": "object", "properties": {}},
    "config": {"api_key": ""},
    "config_schema": {
        "fields": [
            {
                "key": "api_key",
                "label": "Atlassian API Key",
                "type": "password",
                "default": "",
                "placeholder": "ATSTT3x... (service account key) or Basic base64(email:token)",
                "description": (
                    "Service account API key (Bearer) or base64-encoded email:api_token (Basic). "
                    "Get your API key from id.atlassian.com/manage-profile/security/api-tokens"
                ),
            },
        ]
    },
}


async def seed_atlassian_rovo_config():
    """Ensure the Atlassian Rovo platform config tool exists in the database."""
    import os

    env_key = os.environ.get("ATLASSIAN_API_KEY", "").strip()

    async with async_session() as db:
        t = ATLASSIAN_ROVO_CONFIG_TOOL
        result = await db.execute(select(Tool).where(Tool.name == t["name"]))
        existing = result.scalar_one_or_none()
        if not existing:
            initial_config = dict(t["config"])
            if env_key:
                initial_config["api_key"] = env_key
            tool = Tool(
                name=t["name"],
                display_name=t["display_name"],
                description=t["description"],
                type="mcp_config",
                category=t["category"],
                icon=t["icon"],
                is_default=t["is_default"],
                parameters_schema=t["parameters_schema"],
                config=initial_config,
                config_schema=t["config_schema"],
                mcp_server_url=ATLASSIAN_ROVO_MCP_URL,
                mcp_server_name="Atlassian Rovo",
            )
            db.add(tool)
            await db.commit()
            logger.info("[ToolSeeder] Created Atlassian Rovo config tool")
        else:
            updated = False
            if existing.config_schema != t["config_schema"]:
                existing.config_schema = t["config_schema"]
                updated = True
            if existing.mcp_server_url != ATLASSIAN_ROVO_MCP_URL:
                existing.mcp_server_url = ATLASSIAN_ROVO_MCP_URL
                updated = True
            if env_key and (not existing.config or not existing.config.get("api_key")):
                existing.config = {**(existing.config or {}), "api_key": env_key}
                updated = True
            if updated:
                await db.commit()
                logger.info("[ToolSeeder] Updated Atlassian Rovo config tool")


async def get_atlassian_api_key() -> str:
    """Read the Atlassian API key from the platform config tool."""
    async with async_session() as db:
        result = await db.execute(select(Tool).where(Tool.name == "atlassian_rovo"))
        tool = result.scalar_one_or_none()
        if tool and tool.config:
            return tool.config.get("api_key", "")
    return ""
