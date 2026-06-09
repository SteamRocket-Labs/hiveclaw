# Workspace Boundary Reference

The workspace is the durable file surface for reports, artifacts, and working
state. Automatically managed system directories have stricter ownership.

## Safe Workspace Operations

- Read existing files before editing.
- Use precise path searches before creating new files.
- Keep deliverables under `workspace/` unless a skill specifies another path.
- Prefer structured formats for data artifacts.

## Protected Areas

- `memory/learnings/`, `logs/`, and `evolution/` are managed by platform services.
- `soul.md` is not a normal editable document.
- `focus.md` is a personal scratch file, not a source of truth.

## File Delivery

When the user needs a file, create the artifact first, verify it exists, then
use the appropriate channel or file delivery skill.
