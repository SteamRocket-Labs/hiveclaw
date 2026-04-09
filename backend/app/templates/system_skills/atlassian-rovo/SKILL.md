---
name: Atlassian Rovo
description: Jira / Confluence / Compass tool integration guide
---

## Atlassian Rovo Tools

You have access to Atlassian tools through the Rovo MCP integration when this agent has been configured for Atlassian.

## Tool Naming Contract

- Atlassian tools are synced dynamically from the connected site.
- In the current runtime, the synced tool names are exposed with the `atlassian_rovo_` prefix.
- Always use the exact `atlassian_rovo_*` tool names that are present in your current tool list.
- Never invent Jira, Confluence, or Compass tool names from memory.

## How To Work Safely

- If an `atlassian_rovo_*` tool is visible, call it directly through the tool-calling mechanism.
- If no `atlassian_rovo_*` tools are available, treat Atlassian as not configured for this agent.
- Only report completion after receiving a real tool result.

## Important Notes

- Use real identifiers from tool results — don't fabricate Jira issue IDs, Confluence URLs, or Compass names.
- Only claim success when you have a real tool result confirming it.
- If the integration is already configured, no need to ask for credentials.
- Only use `atlassian_rovo_*` tools that are actually present in your current tool list.
