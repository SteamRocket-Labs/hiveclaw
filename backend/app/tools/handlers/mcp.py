"""MCP tools — import, list, and read MCP server resources."""

from __future__ import annotations

import uuid

from app.runtime.prompts.mcp import (
    CALL_MCP_TOOL_DESCRIPTION,
    IMPORT_MCP_SERVER_DESCRIPTION,
    INSPECT_MCP_TOOL_DESCRIPTION,
    LIST_MCP_TOOLS_DESCRIPTION,
    MCP_LIST_RESOURCES_DESCRIPTION,
    MCP_READ_RESOURCE_DESCRIPTION,
)
from app.tools.decorator import ToolMeta, tool
from app.tools.result_envelope import render_tool_error


# -- list_mcp_tools (DB introspection; old name list_mcp_resources kept as alias) ---


@tool(
    ToolMeta(
        name="list_mcp_tools",
        description=LIST_MCP_TOOLS_DESCRIPTION,
        parameters={"type": "object", "properties": {}},
        category="mcp",
        display_name="List MCP Tools",
        icon="\U0001f4cb",
        pack="mcp_admin_pack",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
        # Renamed from list_mcp_resources (it lists imported TOOLS, not protocol
        # resources — those are mcp_list_resources). Old name stays executable.
        aliases=("list_mcp_resources",),
    )
)
async def list_mcp_tools(agent_id: uuid.UUID, arguments: dict) -> str:
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.tool import AgentTool, Tool
    from app.services.tenant_resolver import resolve_tenant_for_agent

    try:
        # RLS 阶段1: scope the `tools` (policy-bearing) read to the agent's tenant.
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(
                select(Tool)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.enabled.is_(True),
                    Tool.type == "mcp",
                    Tool.enabled.is_(True),
                )
            )
            tools = result.scalars().all()
            from app.services.mcp_server_service import resolve_agent_mcp_tool_mode

            # Closure A2: approval gates EXECUTION, not discovery — deny hides
            # the tool, approval keeps it listed with the cost visible up front.
            visible_tools = []
            approval_names: set[str] = set()
            for tool_row in tools:
                mode = await resolve_agent_mcp_tool_mode(db, agent_id, tool_row)
                if mode == "deny":
                    continue
                if mode == "approval":
                    approval_names.add(tool_row.name)
                visible_tools.append(tool_row)
            tools = visible_tools
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
                    marker = " [approval required]" if t.name in approval_names else ""
                    lines.append(f"- **{t.name}**{marker} ({t.display_name}): {t.description[:100]}")
                lines.append("")

            return "\n".join(lines)
    except Exception as exc:
        return render_tool_error(
            tool_name="list_mcp_tools",
            error_class="operation_failed",
            message=f"Failed to list MCP tools: {type(exc).__name__}: {str(exc)[:200]}",
            provider="mcp",
            retryable=True,
            actionable_hint="Retry after the MCP registry or database becomes available.",
        )


# -- inspect_mcp_tool (DB introspection; old name read_mcp_resource kept as alias) --


@tool(
    ToolMeta(
        name="inspect_mcp_tool",
        description=INSPECT_MCP_TOOL_DESCRIPTION,
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
        display_name="Inspect MCP Tool",
        icon="\U0001f50d",
        pack="mcp_admin_pack",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
        # Renamed from read_mcp_resource (it inspects an imported TOOL's schema,
        # not a protocol resource — those are mcp_read_resource). Old name aliased.
        aliases=("read_mcp_resource",),
    )
)
async def inspect_mcp_tool(agent_id: uuid.UUID, arguments: dict) -> str:
    import json

    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.tool import AgentTool, Tool
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tool_name = arguments.get("tool_name", "")
    if not tool_name:
        return render_tool_error(
            tool_name="inspect_mcp_tool",
            error_class="bad_arguments",
            message="tool_name is required.",
            provider="mcp",
            retryable=False,
            actionable_hint="Call list_mcp_tools first, then pass one of the returned MCP tool names.",
        )

    try:
        # RLS 阶段1: scope the `tools` (policy-bearing) read to the agent's tenant.
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(
                select(Tool)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.enabled.is_(True),
                    Tool.name == tool_name,
                    Tool.type == "mcp",
                    Tool.enabled.is_(True),
                )
            )
            t = result.scalar_one_or_none()
            if not t:
                return render_tool_error(
                    tool_name="inspect_mcp_tool",
                    error_class="not_found",
                    message=f"MCP tool '{tool_name}' not found.",
                    provider="mcp",
                    retryable=False,
                    actionable_hint="Use list_mcp_tools to discover currently imported MCP tool names.",
                )
            from app.services.mcp_server_service import resolve_agent_mcp_tool_mode

            mode = await resolve_agent_mcp_tool_mode(db, agent_id, t)
            if mode == "deny":
                return render_tool_error(
                    tool_name="inspect_mcp_tool",
                    error_class="forbidden",
                    message=f"MCP tool '{tool_name}' is denied by this agent's MCP server policy.",
                    provider="mcp",
                    retryable=False,
                    actionable_hint="Enable the MCP server or change the tool policy in advanced MCP controls.",
                )

            info = [
                f"## MCP Tool: {t.name}",
                f"- Display name: {t.display_name}",
                f"- Description: {t.description}",
                f"- Server: {t.mcp_server_name or t.mcp_server_url or 'unknown'}",
                f"- MCP tool name: {t.mcp_tool_name or t.name}",
                f"- Enabled: {t.enabled}",
            ]
            if mode == "approval":
                # Closure A2: visibility ≠ executability — the schema stays
                # readable, but calling this tool requires approval first.
                info.append(
                    "- ⚠️ Policy: calling this tool requires approval — the call will create "
                    "an approval request and wait for a human decision."
                )
            info.append(
                f"- Parameters schema:\n```json\n{json.dumps(t.parameters_schema, indent=2, ensure_ascii=False)}\n```"
            )
            return "\n".join(info)
    except Exception as exc:
        return render_tool_error(
            tool_name="inspect_mcp_tool",
            error_class="operation_failed",
            message=f"Failed to inspect MCP tool: {type(exc).__name__}: {str(exc)[:200]}",
            provider="mcp",
            retryable=True,
            actionable_hint="Retry after the MCP registry or database becomes available.",
        )


# -- import_mcp_server --------------------------------------------------------


@tool(
    ToolMeta(
        name="import_mcp_server",
        description=IMPORT_MCP_SERVER_DESCRIPTION,
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
    )
)
async def import_mcp_server(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _import_mcp_server

    return await _import_mcp_server(agent_id, arguments)


# -- call_mcp_tool ------------------------------------------------------------
# P1-W3-4: until this lands, the agent could only read MCP tool *metadata*
# from the database — there was no path to actually invoke a tool on the
# remote server. This handler fills that gap by resolving the imported
# Tool row, opening an MCPClient session, and forwarding the call.


@tool(
    ToolMeta(
        name="call_mcp_tool",
        description=CALL_MCP_TOOL_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Hive-side name of the imported MCP tool (use list_mcp_tools to discover)",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments matching the MCP tool's input schema",
                },
            },
            "required": ["tool_name"],
        },
        category="mcp",
        display_name="Call MCP Tool",
        icon="\U0001f680",
        pack="mcp_admin_pack",
        adapter="agent_args",
    )
)
async def call_mcp_tool(agent_id: uuid.UUID, arguments: dict) -> str:
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.tool import AgentTool, Tool
    from app.services.mcp_client import MCPClient
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tool_name = arguments.get("tool_name", "")
    tool_args = arguments.get("arguments") or {}

    if not tool_name:
        return render_tool_error(
            tool_name="call_mcp_tool",
            error_class="bad_arguments",
            message="tool_name is required.",
            provider="mcp",
            retryable=False,
            actionable_hint="Discover MCP tool names via list_mcp_tools.",
        )

    if not isinstance(tool_args, dict):
        return render_tool_error(
            tool_name="call_mcp_tool",
            error_class="bad_arguments",
            message="`arguments` must be an object matching the MCP tool's input schema.",
            provider="mcp",
            retryable=False,
            actionable_hint="Re-read the schema via inspect_mcp_tool and rebuild the arguments dict.",
        )

    # RLS 阶段1: scope the `tools` (policy-bearing) read to the agent's tenant.
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        result = await db.execute(
            select(Tool)
            .join(AgentTool, AgentTool.tool_id == Tool.id)
            .where(
                AgentTool.agent_id == agent_id,
                AgentTool.enabled.is_(True),
                Tool.name == tool_name,
                Tool.type == "mcp",
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return render_tool_error(
                tool_name="call_mcp_tool",
                error_class="not_found",
                message=f"MCP tool '{tool_name}' is not imported. Use import_mcp_server first.",
                provider="mcp",
                retryable=False,
                actionable_hint="Use list_mcp_tools to see what's available.",
            )
        if not row.enabled:
            return render_tool_error(
                tool_name="call_mcp_tool",
                error_class="forbidden",
                message=f"MCP tool '{tool_name}' is disabled.",
                provider="mcp",
                retryable=False,
                actionable_hint="Have a platform admin re-enable it from the tools panel.",
            )
        from app.services.mcp_server_service import resolve_agent_mcp_tool_mode

        mode = await resolve_agent_mcp_tool_mode(db, agent_id, row)
        if mode == "deny":
            return render_tool_error(
                tool_name="call_mcp_tool",
                error_class="forbidden",
                message=f"MCP tool '{tool_name}' is denied by this agent's MCP server policy.",
                provider="mcp",
                retryable=False,
                actionable_hint="Enable the MCP server or change the tool policy in advanced MCP controls.",
            )
        if not row.mcp_server_url:
            return render_tool_error(
                tool_name="call_mcp_tool",
                error_class="bad_state",
                message=f"MCP tool '{tool_name}' has no server URL on file.",
                provider="mcp",
                retryable=False,
                actionable_hint="Re-import the server via import_mcp_server.",
            )

        server_url = row.mcp_server_url
        api_key = (row.config or {}).get("api_key") if isinstance(row.config, dict) else None
        remote_name = row.mcp_tool_name or row.name

    try:
        client = MCPClient(server_url, api_key=api_key)
        return await client.call_tool(remote_name, tool_args)
    except Exception as exc:
        return render_tool_error(
            tool_name="call_mcp_tool",
            error_class="operation_failed",
            message=f"MCP call failed: {type(exc).__name__}: {str(exc)[:200]}",
            provider="mcp",
            retryable=True,
            actionable_hint="Check the MCP server is reachable and the API key is valid.",
        )


# -- MCP protocol resources (resources/list + resources/read) -----------------
# Distinct from list_mcp_tools/inspect_mcp_tool (which introspect imported TOOLS
# from the DB): these reach the live server for its first-class *resources*.


async def _resolve_agent_mcp_server(agent_id: uuid.UUID, server: str | None) -> tuple[str, str | None] | str:
    """Resolve (server_url, api_key) for one MCP server the agent may reach.

    Server access follows tool access: the agent must hold at least one enabled,
    non-denied imported tool from that server. ``server`` matches
    ``mcp_server_name``; when omitted and the agent has exactly one MCP server it
    is used, otherwise an actionable error lists the choices. Returns the tuple or
    a rendered-error string.
    """
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.tool import AgentTool, Tool
    from app.services.mcp_server_service import resolve_agent_mcp_tool_mode
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        rows = (
            (
                await db.execute(
                    select(Tool)
                    .join(AgentTool, AgentTool.tool_id == Tool.id)
                    .where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.enabled.is_(True),
                        Tool.type == "mcp",
                        Tool.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_server: dict[str, tuple[str, Tool]] = {}
        for row in rows:
            url = row.mcp_server_url
            if not url:
                continue
            if await resolve_agent_mcp_tool_mode(db, agent_id, row) == "deny":
                continue
            name = row.mcp_server_name or url
            by_server.setdefault(name, (url, row))

    if not by_server:
        return render_tool_error(
            tool_name="mcp_list_resources",
            error_class="not_found",
            message="This agent has no reachable MCP server.",
            provider="mcp",
            retryable=False,
            actionable_hint="Import an MCP server with import_mcp_server first.",
        )
    if server:
        chosen = by_server.get(server)
        if chosen is None:
            return render_tool_error(
                tool_name="mcp_list_resources",
                error_class="not_found",
                message=f"No reachable MCP server named '{server}'.",
                provider="mcp",
                retryable=False,
                actionable_hint=f"Available servers: {', '.join(sorted(by_server))}.",
            )
    elif len(by_server) == 1:
        chosen = next(iter(by_server.values()))
    else:
        return render_tool_error(
            tool_name="mcp_list_resources",
            error_class="bad_arguments",
            message="Multiple MCP servers are available; specify which one.",
            provider="mcp",
            retryable=False,
            actionable_hint=f"Pass server=one of: {', '.join(sorted(by_server))}.",
        )
    url, row = chosen
    api_key = (row.config or {}).get("api_key") if isinstance(row.config, dict) else None
    return url, api_key


@tool(
    ToolMeta(
        name="mcp_list_resources",
        description=MCP_LIST_RESOURCES_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "server": {
                    "type": "string",
                    "description": "MCP server name (optional when only one server is imported).",
                },
            },
        },
        category="mcp",
        display_name="List MCP Resources",
        icon="\U0001f5c2",
        pack="mcp_admin_pack",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
    )
)
async def mcp_list_resources(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.mcp_client import MCPClient

    resolved = await _resolve_agent_mcp_server(agent_id, arguments.get("server"))
    if isinstance(resolved, str):
        return resolved
    server_url, api_key = resolved
    try:
        resources = await MCPClient(server_url, api_key=api_key).list_resources()
    except Exception as exc:
        return render_tool_error(
            tool_name="mcp_list_resources",
            error_class="operation_failed",
            message=f"Failed to list MCP resources: {type(exc).__name__}: {str(exc)[:200]}",
            provider="mcp",
            retryable=True,
            actionable_hint="Check the MCP server is reachable and authorized.",
        )
    if not resources:
        return "This MCP server exposes no resources."
    lines = [f"## MCP Resources ({len(resources)})\n"]
    for r in resources:
        desc = f": {r['description'][:120]}" if r.get("description") else ""
        mime = f" [{r['mimeType']}]" if r.get("mimeType") else ""
        lines.append(f"- `{r['uri']}` — {r.get('name') or r['uri']}{mime}{desc}")
    return "\n".join(lines)


@tool(
    ToolMeta(
        name="mcp_read_resource",
        description=MCP_READ_RESOURCE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Resource URI from mcp_list_resources."},
                "server": {
                    "type": "string",
                    "description": "MCP server name (optional when only one server is imported).",
                },
            },
            "required": ["uri"],
        },
        category="mcp",
        display_name="Read MCP Resource",
        icon="\U0001f4c4",
        pack="mcp_admin_pack",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
    )
)
async def mcp_read_resource(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.mcp_client import MCPClient

    uri = arguments.get("uri", "")
    if not uri:
        return render_tool_error(
            tool_name="mcp_read_resource",
            error_class="bad_arguments",
            message="uri is required.",
            provider="mcp",
            retryable=False,
            actionable_hint="Call mcp_list_resources first, then pass one of the returned URIs.",
        )
    resolved = await _resolve_agent_mcp_server(agent_id, arguments.get("server"))
    if isinstance(resolved, str):
        return resolved
    server_url, api_key = resolved
    try:
        return await MCPClient(server_url, api_key=api_key).read_resource(uri)
    except Exception as exc:
        return render_tool_error(
            tool_name="mcp_read_resource",
            error_class="operation_failed",
            message=f"Failed to read MCP resource: {type(exc).__name__}: {str(exc)[:200]}",
            provider="mcp",
            retryable=True,
            actionable_hint="Check the URI and that the MCP server is reachable and authorized.",
        )
