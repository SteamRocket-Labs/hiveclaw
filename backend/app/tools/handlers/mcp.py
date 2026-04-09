"""MCP tools — import, list, and read MCP server resources."""

from __future__ import annotations

import uuid

from app.tools.decorator import ToolMeta, tool
from app.tools.result_envelope import render_tool_error


# -- list_mcp_resources -------------------------------------------------------

@tool(ToolMeta(
    name="list_mcp_resources",
    description="List all MCP servers and their tools currently available to this agent.",
    parameters={"type": "object", "properties": {}},
    category="mcp",
    display_name="List MCP Resources",
    icon="\U0001f4cb",
    pack="mcp_admin_pack",
    adapter="agent_args",
))
async def list_mcp_resources(agent_id: uuid.UUID, arguments: dict) -> str:
    from sqlalchemy import select

    from app.database import async_session
    from app.models.tool import AgentTool, Tool

    try:
        async with async_session() as db:
            result = await db.execute(
                select(Tool)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(AgentTool.agent_id == agent_id, Tool.type == "mcp", Tool.enabled.is_(True))
            )
            tools = result.scalars().all()
            if not tools:
                return "No MCP resources found for this agent. Use import_mcp_server to add one."

            lines = [f"## MCP Resources ({len(tools)} tools)\n"]
            by_server: dict[str, list] = {}
            for t in tools:
                server = t.mcp_server_name or t.mcp_server_url or "unknown"
                by_server.setdefault(server, []).append(t)

            for server, server_tools in by_server.items():
                lines.append(f"### Server: {server}")
                for t in server_tools:
                    lines.append(f"- **{t.name}** ({t.display_name}): {t.description[:100]}")
                lines.append("")

            return "\n".join(lines)
    except Exception as exc:
        return render_tool_error(
            tool_name="list_mcp_resources",
            error_class="operation_failed",
            message=f"Failed to list MCP resources: {type(exc).__name__}: {str(exc)[:200]}",
            provider="mcp",
            retryable=True,
            actionable_hint="Retry after the MCP registry or database becomes available.",
        )


# -- read_mcp_resource --------------------------------------------------------

@tool(ToolMeta(
    name="read_mcp_resource",
    description="Read detailed information about a specific MCP tool, including its parameters schema and server configuration.",
    parameters={
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Name of the MCP tool to inspect",
            },
        },
        "required": ["tool_name"],
    },
    category="mcp",
    display_name="Read MCP Resource",
    icon="\U0001f50d",
    pack="mcp_admin_pack",
    adapter="agent_args",
))
async def read_mcp_resource(agent_id: uuid.UUID, arguments: dict) -> str:
    import json

    from sqlalchemy import select

    from app.database import async_session
    from app.models.tool import Tool

    tool_name = arguments.get("tool_name", "")
    if not tool_name:
        return render_tool_error(
            tool_name="read_mcp_resource",
            error_class="bad_arguments",
            message="tool_name is required.",
            provider="mcp",
            retryable=False,
            actionable_hint="Call list_mcp_resources first, then pass one of the returned MCP tool names.",
        )

    try:
        async with async_session() as db:
            result = await db.execute(
                select(Tool).where(Tool.name == tool_name, Tool.type == "mcp")
            )
            t = result.scalar_one_or_none()
            if not t:
                return render_tool_error(
                    tool_name="read_mcp_resource",
                    error_class="not_found",
                    message=f"MCP tool '{tool_name}' not found.",
                    provider="mcp",
                    retryable=False,
                    actionable_hint="Use list_mcp_resources to discover currently imported MCP tool names.",
                )

            info = [
                f"## MCP Tool: {t.name}",
                f"- Display name: {t.display_name}",
                f"- Description: {t.description}",
                f"- Server: {t.mcp_server_name or t.mcp_server_url or 'unknown'}",
                f"- MCP tool name: {t.mcp_tool_name or t.name}",
                f"- Enabled: {t.enabled}",
                f"- Parameters schema:\n```json\n{json.dumps(t.parameters_schema, indent=2, ensure_ascii=False)}\n```",
            ]
            return "\n".join(info)
    except Exception as exc:
        return render_tool_error(
            tool_name="read_mcp_resource",
            error_class="operation_failed",
            message=f"Failed to read MCP resource: {type(exc).__name__}: {str(exc)[:200]}",
            provider="mcp",
            retryable=True,
            actionable_hint="Retry after the MCP registry or database becomes available.",
        )


# -- import_mcp_server --------------------------------------------------------

@tool(ToolMeta(
    name="import_mcp_server",
    description="Import an MCP server from Smithery registry into the platform. Treat this as an explicit platform-extension workflow, not a normal task-execution step. Use discover_resources first to find the server ID. If previously imported tools stopped working (e.g. OAuth expired), set reauthorize=true to re-run the authorization flow.",
    parameters={
        "type": "object",
        "properties": {
            "server_id": {
                "type": "string",
                "description": "Smithery server ID, e.g. '@anthropic/brave-search' or '@anthropic/fetch'",
            },
            "config": {
                "type": "object",
                "description": "Optional server configuration (e.g. API keys required by the server)",
            },
            "reauthorize": {
                "type": "boolean",
                "description": "Set to true to force re-authorization of existing tools (e.g. when OAuth token has expired)",
            },
        },
        "required": ["server_id"],
    },
    category="mcp",
    display_name="Import MCP Server",
    icon="\U0001f4e6",
    pack="mcp_admin_pack",
    adapter="agent_args",
))
async def import_mcp_server(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _import_mcp_server
    return await _import_mcp_server(agent_id, arguments)
