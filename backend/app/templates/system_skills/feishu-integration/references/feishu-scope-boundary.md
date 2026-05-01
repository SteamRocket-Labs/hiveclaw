# Feishu Scope Boundary

Feishu work must go through configured `feishu_*` tools. OpenAPI scopes,
tenant channel configuration, and user identifiers must come from the platform
or tool responses.

## Identifier Order

1. Use explicit `user_id` if provided.
2. Use explicit `open_id` if provided.
3. Search by name with `feishu_user_search`.
4. Ask the user to disambiguate when multiple matches exist.

## Mutation Rules

- Confirm destructive actions such as delete operations.
- Check doc/wiki/base/table identifiers before writing.
- Report missing scopes exactly as returned by the tool.
- Never fall back to shell credential probing.

## Common Scope Families

- Messaging: IM send permissions.
- Docs/Wiki: document read/write/share/delete.
- Sheets/Base: spreadsheet and bitable scopes.
- Calendar/Task/Approval: app-specific operation scopes.
