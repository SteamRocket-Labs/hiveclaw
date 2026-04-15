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

<role>
Use this skill when the user wants to add an external integration via the
MCP (Model Context Protocol) mechanism — either from a hosted registry
entry (Smithery-style catalog) or a direct MCP HTTP/SSE URL. The skill
also covers inspecting what an imported server exposes and reading
specific MCP resources.
</role>

<when_to_use>
- User wants to connect an external system (GitHub, Notion, Linear, a custom API) that is NOT already served by a platform skill or builtin
- User already has an MCP URL and wants to import it
- User wants to see what resources an imported MCP server exposes
- User wants to read a specific MCP resource payload
</when_to_use>

<do_not_use_when>
- The need is already covered by a platform skill (Feishu, DingTalk, Atlassian, Email) → use that skill instead
- The need is covered by builtin tools (web_search, web_fetch, file I/O) → no MCP import needed
- The target runs only on the user's local workstation (requires Docker, local binary) — not importable from the platform runtime
- The user hasn't actually asked for a new integration — don't pre-install MCP servers speculatively
</do_not_use_when>

## Tool Reference

<tool_reference>

| I need to... | Use | Key Params |
|-------------|-----|------------|
| Find a candidate MCP integration | `discover_resources` | `query` (free text), optional `max_results` |
| Import a server into the runtime | `import_mcp_server` | `server_id`, `config` (dict with `mcp_url` or registry auth) |
| Inspect what an imported server exposes | `list_mcp_resources` | (none required) |
| Read a specific MCP resource payload | `read_mcp_resource` | `uri` (from `list_mcp_resources` output) |

</tool_reference>

## Workflow

<workflows>

### 1. Search first

```python
discover_resources(query="<what the user wants>", max_results=5)
```
Show the candidates, explain the best match, and let the user confirm which one to import.

### 2. Choose the import path

**Hosted registry entry** — Use when the server is listed in the MCP registry / Smithery-style catalog. Prefer this path because auth and metadata are usually standardized.

**Direct URL import** — Use when the user already has a public MCP HTTP/SSE endpoint. They may need to provide endpoint-specific config or an API key.

**Not importable** — Local-only servers that require Docker, local binaries, or a workstation process. If the server cannot be reached from the platform runtime, explain that limitation instead of pretending import worked.

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

```python
list_mcp_resources()
```
If the user wants a specific resource, read it directly:
```python
read_mcp_resource(uri="mcp://...")
```

### 5. Hosted registry auth guidance

If a hosted provider requires a registry key, explain the minimum context:
> A Smithery / registry API key identifies the account that owns the imported integration and helps complete hosted OAuth or provisioning safely. Provide it once, then future imports can usually reuse that configuration.

- Do not ask for GitHub PAT, Notion API key, or other per-product secrets when the hosted flow already supports OAuth.
- Do not echo API keys back to the user.
- If OAuth returns an authorization URL, tell the user to open it and finish auth.

</workflows>

## Examples

<examples>

### Example A — Import a hosted Notion integration

Input: `帮我接一下 Notion，我要能查询我的数据库`

Correct flow:
```
discover_resources(query="Notion database")
  → top hit: server_id="smithery/notion-official", description="Official Notion integration via OAuth"
# Explain to user, ask for confirmation + smithery_api_key
import_mcp_server(server_id="smithery/notion-official",
                  config={"smithery_api_key": "<user-provided>"})
  → returns: authorization_url="https://api.smithery.ai/oauth/..."
# Tell user to open the URL and finish OAuth.
# Then:
list_mcp_resources()
  → shows: notion_search_pages, notion_query_database, ...
```
Output: `已导入 Notion 集成。请打开 <url> 完成 OAuth 授权，回来后我可以用 notion_search_pages 等工具帮你查数据库。`

### Example B — Direct URL import

Input: `我有一个自己的 MCP 服务在 https://my-tools.example.com/sse，接进来`

Correct flow:
```
import_mcp_server(server_id="my-custom-tools",
                  config={"mcp_url": "https://my-tools.example.com/sse"})
  → success, tools visible
list_mcp_resources()
  → shows the actual tool surface
```
Output: `已接入 https://my-tools.example.com/sse。新工具有：<list>。试试哪个？`

### Example C — Refuse a local-only server

Input: `接一下这个 MCP 服务：docker run local-tool`

Correct response: `这个 MCP 服务只能在本地机器上跑（需要 Docker），平台运行时没法直接访问。可选路径：(1) 把它部署到公网 HTTPS 端点再用 URL 导入；(2) 如果只是需要执行本地命令，用 execute_code；(3) 看有没有等价的托管版本（我帮你 discover_resources 搜一下）。`

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Skip `discover_resources` and guess a server_id from memory** → registry IDs change; your call fails with an obscure error. Always search first.
- ❌ **Claim "imported" without verifying with `list_mcp_resources`** → `import_mcp_server` can return a soft success while the runtime hasn't actually connected. Always verify.
- ❌ **Ask the user for an API key before checking if OAuth is available** → duplicates work and may leak secrets unnecessarily. Check if the provider uses OAuth first.
- ❌ **Echo API keys or OAuth tokens back to the user** → they're secrets; they should stay in tool config only.
- ❌ **Pretend a Docker-only server was imported** → the runtime cannot reach local binaries. Report the limitation honestly.
- ❌ **Fabricate MCP URIs** in `read_mcp_resource` calls → they must come from `list_mcp_resources` output.
- ❌ **Import an MCP server when a platform skill already exists** for the same system (e.g. importing a Feishu MCP when `feishu-integration` is installed). Platform skills are first-class; MCP is a last resort.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every import is preceded by a real `discover_resources` call (or an explicit user-provided URL).
- Every "imported successfully" claim is backed by `list_mcp_resources` output in this session.
- Every MCP resource URI in your output came from a real tool response, not memory.
- API keys and OAuth tokens never appear in your text output to the user.
- When a platform skill exists for the same system, the user is told to use that skill instead.
</success_criteria>
