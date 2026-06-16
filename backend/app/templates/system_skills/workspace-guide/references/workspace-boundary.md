# Workspace Boundary Reference

The workspace is the durable file surface for reports, artifacts, and working
state. Automatically managed system directories have stricter ownership.

## Safe Workspace Operations

- Read existing files before editing.
- Use precise path searches before creating new files.
- Keep deliverables under `workspace/` unless a skill specifies another path.
- Prefer structured formats for data artifacts.
- Rediscover uploaded files under `workspace/uploads/`.
- Rediscover Deep Research deliverables under `workspace/deep_research_reports/`.
- Rediscover oversized tool-result spill files under `workspace/tool_results/`.
- `run_command` executes from `workspace/`; files created by the command are
  found under that directory.

## Protected Areas

- `memory/learnings/`, `logs/`, and `evolution/` are managed by platform services.
- `soul.md` is not a normal editable document.
- `runtime_artifacts/` is recovery and audit evidence. Read it only when a tool
  result or recovery task points there; prefer mirrored user-facing files under
  `workspace/` when available.

## File Delivery

When the user needs a file, create the artifact first, verify it exists, then
use the appropriate channel or file delivery skill.
