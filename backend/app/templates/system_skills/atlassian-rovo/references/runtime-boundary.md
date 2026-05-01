# Atlassian Rovo Runtime Boundary

Atlassian capabilities are discovered dynamically from the connected Rovo MCP
server. Do not invent Jira, Confluence, or Compass tool names from memory.

## Required Checks

1. Confirm at least one visible tool starts with `atlassian_rovo_`.
2. Match the user's intent to the exact visible tool schema.
3. Use IDs returned by the tool response, such as issue keys or page IDs.
4. Stop on auth or configuration failure and report the integration gap.

## Boundaries

- Credentials live in the integration configuration, not shell environment.
- Do not scrape authenticated Jira or Confluence pages as a workaround.
- Do not assume all Atlassian sites expose the same Rovo tool set.
- Mutations require explicit user intent and a visible mutation tool.
