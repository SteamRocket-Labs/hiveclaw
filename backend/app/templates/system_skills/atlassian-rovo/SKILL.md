---
name: Atlassian Rovo
description: "Use when Codex needs to operate a configured Atlassian Rovo integration for Jira or Confluence tasks, verify runtime tool availability, and avoid unsupported authenticated web scraping fallbacks."
---

# Atlassian Rovo Tools

<role>
Use this skill when the user wants to read, query, create, or update items
in Jira (issues, sprints, projects), Confluence (pages, spaces), or Compass
(components, scorecards). Atlassian access is delivered via the Rovo MCP
integration — tools are **synced dynamically** from the connected site, so
the exact tool names depend on runtime state rather than a fixed list.
</role>

<when_to_use>
- User asks about a Jira issue, project, sprint, or board
- User asks to read, search, or create a Confluence page
- User asks about a Compass component, service, or scorecard
- User wants to file a new Jira ticket or comment on an existing one
- User provides a Jira URL or Confluence URL and wants you to do something with it
</when_to_use>

<do_not_use_when>
- No `atlassian_rovo_*` tools are visible in your current tool list → Atlassian is not configured for this agent; tell the user and stop
- The user wants to use GitHub, GitLab, or another non-Atlassian issue tracker — a different skill/integration owns those
- The user only wants general knowledge about Jira/Confluence features — answer from knowledge instead of calling tools
</do_not_use_when>

## Credential Boundary

- Atlassian credentials are owned by the connected Rovo MCP/tool config for this agent or tenant.
- Do not inspect environment variables or use `run_command` to look for Jira, Confluence, Atlassian, OAuth, token, or API key values.
- If no `atlassian_rovo_*` tool is visible or a tool reports auth/config failure, report the configuration gap and stop; do not bypass the integration through shell/env probing.

## Tool Reference

<tool_reference>

Atlassian tools are **not statically declared** in this SKILL.md because
they're synced from the connected Rovo MCP server. At runtime they appear
in your tool list with the `atlassian_rovo_` prefix. Typical examples you
may see (names depend on what the connected site exposes):

| Typical task | Expected tool shape |
|--------------|---------------------|
| Read a Jira issue | `atlassian_rovo_get_jira_issue` or similar |
| Search Jira issues | `atlassian_rovo_search_jira_issues` or similar |
| Create a Jira issue | `atlassian_rovo_create_jira_issue` or similar |
| Comment on a Jira issue | `atlassian_rovo_add_jira_comment` or similar |
| Read a Confluence page | `atlassian_rovo_get_confluence_page` or similar |
| Search Confluence | `atlassian_rovo_search_confluence` or similar |
| Query Compass components | `atlassian_rovo_*` (Compass tools vary widely) |

**Rule of thumb**: look at your current tool list, find the
`atlassian_rovo_*` tool whose name matches your intent, and call it.
Never invent a name from memory.

</tool_reference>

## Workflow

<workflows>

### 1. Verify the integration is live
Scan your current tool list for any `atlassian_rovo_*` tool.
- If none present → report: "Atlassian Rovo is not configured for this agent. Ask an admin to install the Atlassian integration."
- If present → proceed.

### 2. Match intent to tool
From the visible `atlassian_rovo_*` tool list, pick the one whose name best matches the user's ask. Read the tool's own parameter schema (returned by tool-listing introspection) to know exactly what arguments to pass.

### 3. Call and cite
Call the tool. Use the real identifiers from its response (issue keys like `PROJ-123`, page IDs, component names) when telling the user what you did.

### 4. Never cross the boundary
If a requested action has no matching `atlassian_rovo_*` tool in your current list, report the gap. Do not attempt to accomplish it via `web_fetch` of the Jira/Confluence web UI unless the user explicitly asks for a read-only workaround.

</workflows>

## Examples

<examples>

### Example A — Read a Jira issue
Input: `帮我看下 PROJ-142 现在什么状态`
Step 1: Confirm `atlassian_rovo_*` tools exist. Find one like `atlassian_rovo_get_jira_issue`.
Step 2: Call `atlassian_rovo_get_jira_issue(issue_key="PROJ-142")` (exact params depend on the tool's own schema).
Step 3: Report the status, assignee, and last update time from the response, linking the real issue URL.

### Example B — Atlassian is NOT configured
Input: `帮我建一个 Confluence 页面写周会纪要`
Correct response: `这个数字员工目前没有安装 Atlassian 集成（工具列表里没有任何 atlassian_rovo_* 工具）。请管理员在"企业设置 → 集成"里配置 Atlassian Rovo 后再试。要不要我先把纪要写到 workspace/ 里，等 Atlassian 配好再同步？`

</examples>

## Anti-patterns

<anti_patterns>
- ❌ **Invent a tool name from memory** like `atlassian_rovo_jira_search` when the actually-synced name is `atlassian_rovo_search_issues`. The call fails loudly. Always read the live tool list first.
- ❌ **Call `web_fetch` on a Jira URL to work around missing tools without telling the user** → silently bypasses the governance layer and produces stale/partial data. If `atlassian_rovo_*` is missing, report it and let the user choose.
- ❌ **Fabricate Jira issue keys, Confluence page IDs, or Compass component names** → Atlassian rejects them or returns empty. Only use identifiers that appear in real tool responses or user messages.
- ❌ **Claim "issue created" based on your own text output** → only claim success when a tool response returned a real issue key and URL.
- ❌ **Mix Atlassian tools with GitHub/GitLab semantics** → Atlassian workflows, fields, and states differ; don't assume PRs map to Jira issues automatically.
</anti_patterns>

## Success Criteria

<success_criteria>
- Every Jira issue key, Confluence page ID, or Compass identifier in your output came from a real `atlassian_rovo_*` tool response in this session.
- If the integration is not configured, you reported the missing state explicitly and offered a useful fallback (write to workspace, message the user later, etc.).
- You never asserted "done" for an Atlassian action without a tool response backing it.
</success_criteria>

## Bundled Resources

Load resources by need, not by default:

- `references/runtime-boundary.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/rovo-action-report.md`: use as the output scaffold when creating this artifact type.
