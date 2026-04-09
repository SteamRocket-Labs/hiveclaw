---
name: MCP Tool Installer
description: Discover, import, inspect, and read MCP integrations directly in chat when users need new external tools or resources.
tools:
  - discover_resources
  - import_mcp_server
  - list_mcp_resources
  - read_mcp_resource
is_system: true
---

# MCP Tool Installer

Use this skill when the user wants to add an external integration, inspect what an imported MCP server exposes, or read a resource from an MCP server.

## When to Use Which Tool

| I need to... | Use |
|-------------|-----|
| Find a candidate MCP integration | `discover_resources` |
| Import a server into the runtime | `import_mcp_server` |
| Inspect what an imported server exposes | `list_mcp_resources` |
| Read a specific MCP resource payload | `read_mcp_resource` |

## Workflow

### 1. Search first
```python
discover_resources(query="<what the user wants>", max_results=5)
```
Show the candidates, explain the best match, and let the user confirm which one to import.

### 2. Choose the import path

**Hosted registry entry**
- Use when the server is listed in the MCP registry / Smithery-style catalog
- Prefer this path because auth and metadata are usually standardized

**Direct URL import**
- Use when the user already has a public MCP HTTP/SSE endpoint
- The user may need to provide endpoint-specific config or an API key

**Not importable here**
- Local-only servers that require Docker, local binaries, or a workstation process
- If the server cannot be reached from the platform runtime, explain that limitation instead of pretending import worked

### 3. Import

Hosted entry:
```python
import_mcp_server(
  server_id="<qualified_name>",
  config={"smithery_api_key": "<key>"}  # only if the provider requires it
)
```

Direct URL:
```python
import_mcp_server(
  server_id="<display-name>",
  config={
    "mcp_url": "https://example.com/sse",
    "api_key": "<optional provider key>"
  }
)
```

### 4. Verify after import

After import, verify what the server actually exposes:

```python
list_mcp_resources()
```

If the user wants a specific resource, read it directly:

```python
read_mcp_resource(uri="mcp://...")
```

## Hosted Registry Guidance

If a hosted provider requires a registry key, explain the minimum context:

> A Smithery / registry API key identifies the account that owns the imported integration and helps complete hosted OAuth or provisioning safely. Provide it once, then future imports can usually reuse that configuration.

Important:
- Do not ask for GitHub PAT, Notion API key, or other per-product secrets when the hosted flow already supports OAuth
- Do not echo API keys back to the user
- If OAuth returns an authorization URL, tell the user to open it and finish auth

## What Good Looks Like

- You searched before importing
- You used the real server id or URL
- You verified the import with `list_mcp_resources`
- You used `read_mcp_resource` when the user asked for actual resource contents
- You reported any auth/runtime limitation honestly

## Important Notes

- Always search with `discover_resources` before importing — let the user choose from real candidates
- Only claim import succeeded when you have a real tool result confirming it
- Use real server IDs and URLs from tool results — don't fabricate MCP endpoints or OAuth states
- Complete the flow in chat when possible — avoid sending the user to Settings unnecessarily
