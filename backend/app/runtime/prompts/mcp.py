"""Tool descriptions for MCP discovery, tool calls, and protocol resources."""

from __future__ import annotations


LIST_MCP_TOOLS_DESCRIPTION = (
    "List imported MCP tools currently available to this agent. This lists tool "
    "schemas, not protocol resources; use mcp_list_resources for resources/list."
)

INSPECT_MCP_TOOL_DESCRIPTION = (
    "Inspect one imported MCP tool's parameters schema before calling it, including "
    "server metadata and approval policy."
)

IMPORT_MCP_SERVER_DESCRIPTION = (
    "Import an MCP server from Smithery registry into the platform. Treat this as "
    "an explicit platform-extension workflow, not a normal task-execution step. "
    "Use discover_resources first to find the server ID. If previously imported "
    "tools stopped working, set reauthorize=true to re-run authorization."
)

CALL_MCP_TOOL_DESCRIPTION = (
    "Invoke an imported MCP tool against its remote server. Use call only after "
    "list_mcp_tools/inspect_mcp_tool confirms the tool name and schema. Pass "
    "`tool_name` and an `arguments` dict matching the tool's input schema; the "
    "result is returned as a string."
)

MCP_LIST_RESOURCES_DESCRIPTION = (
    "List the first-class resources an imported MCP server exposes via resources/list, "
    "distinct from tools and tool schemas."
)

MCP_READ_RESOURCE_DESCRIPTION = (
    "Read one MCP server resource by URI via resources/read. Large binary blobs spill to workspace artifacts."
)

MCP_LIST_PROMPTS_DESCRIPTION = (
    "List the first-class prompt templates an imported MCP server exposes via prompts/list, "
    "distinct from tools and resources."
)

MCP_GET_PROMPT_DESCRIPTION = (
    "Render one MCP prompt template via prompts/get. Use mcp_list_prompts first, then pass "
    "the prompt_name and any required arguments."
)

MCP_AUTH_STATUS_DESCRIPTION = (
    "Inspect MCP server authorization status without exposing tokens. Use this when an MCP "
    "tool, resource, or prompt fails due to authorization."
)
